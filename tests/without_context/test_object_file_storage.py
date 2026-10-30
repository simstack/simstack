import hashlib
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from bson import BSON

from simstack.models.files import FileStack
from simstack.models.file_instance import FileInstance
from simstack.models.parameters import Resource
from simstack.util.object_transfer import upload_object, download_object, ObjectFileClient
from simstack.util.file_transfer_client import FileTransferError


@pytest.fixture
def object_http():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            received["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(received["body"])))
            self.end_headers()
            self.wfile.write(received["body"])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}/bucket/key?signature=test", received
    server.shutdown()
    worker.join()


@pytest.mark.parametrize("size", [0, 33, 8 * 1024 * 1024 + 9])
def test_direct_streaming_put_get_checksums_without_api_token(tmp_path, object_http, size):
    url, received = object_http
    source = tmp_path / "source"
    source.write_bytes(b"x" * size)
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    upload_object(url, source, {"x-amz-meta-sha256": checksum})
    result = download_object({"download_url": url, "size_bytes": size, "checksum_sha256": checksum}, tmp_path / "target")
    assert result.path.read_bytes() == source.read_bytes()
    assert received["authorization"] is None


def test_corrupt_download_never_publishes_cache_file(tmp_path, object_http):
    url, received = object_http
    received["body"] = b"wrong"
    with pytest.raises(FileTransferError, match="checksum mismatch"):
        download_object({"download_url": url, "size_bytes": 5, "checksum_sha256": "0" * 64}, tmp_path / "target")
    assert list(tmp_path.iterdir()) == []


def configure_context(tmp_path, monkeypatch):
    monkeypatch.setattr("simstack.core.context.context",
        SimpleNamespace(config=SimpleNamespace(workdir=tmp_path, resource=Resource(value="local"))))


def test_existing_local_file_never_requests_api(tmp_path, monkeypatch):
    configure_context(tmp_path, monkeypatch)
    source = tmp_path / "file"
    source.write_text("cached")
    value = FileStack(name="file", locations=[FileInstance(path="file", resource="local", created_at=datetime.now())])
    monkeypatch.setattr(value.__class__, "_get_via_server_transfer", lambda *args: pytest.fail("API called for local file"))
    assert value.get_raw(Resource(value="local")) == source


def test_missing_local_file_falls_back_to_s3(tmp_path, monkeypatch):
    configure_context(tmp_path, monkeypatch)
    value = FileStack(name="file", locations=[
        FileInstance(path="gone", resource="local", created_at=datetime.now()),
        FileInstance(path="key", resource="__s3__", created_at=datetime.now(), location_type="s3", bucket="bucket", object_key="key")])
    called = []
    monkeypatch.setattr(FileStack, "_get_via_server_transfer", lambda self, source, *args: called.append(source.location_type) or tmp_path / "downloaded")
    assert value.get_raw(Resource(value="local")).name == "downloaded"
    assert called == ["s3"]


def test_file_constructors_preserve_workflow_api_without_bson_payload(tmp_path, monkeypatch):
    configure_context(tmp_path, monkeypatch)
    checksum = hashlib.sha256(b"data").hexdigest()
    class Client:
        def publish(self, file_stack, path):
            assert path.read_bytes() == b"data"
            return FileInstance(path="key", resource="__s3__", created_at=datetime.now(), location_type="s3",
                                bucket="bucket", object_key="key", checksum_sha256=checksum, size_bytes=4).model_dump()
    monkeypatch.setattr(ObjectFileClient, "from_context", lambda **kwargs: Client())
    value = FileStack.from_string("data", "input.txt")
    assert all(location.location_type == "local_path" for location in value.locations)
    value.ensure_uploaded()
    assert value.name == "input.txt"
    assert value.hash == checksum
    assert value.content is None and value.in_memory is False
    assert all(location.location_type == "s3" for location in value.locations)
    document = BSON.encode(value.model_dump_doc())
    assert b"data" not in document
    assert all(location.checksum_sha256 == checksum for location in value.locations)


@pytest.mark.parametrize("payload", [{"content": b"x"}, {"in_memory": True}])
def test_manual_embedded_bytes_are_rejected(payload):
    with pytest.raises(ValueError, match="S3"):
        FileStack(name="unsafe", **payload)


def test_expired_download_url_is_refreshed(tmp_path, monkeypatch):
    from simstack.util.file_transfer_client import FileTransferClient
    import simstack.util.object_transfer as transfers
    client = FileTransferClient(server_url="https://api.test", runner_token="private")
    requested = []
    def metadata(*args):
        requested.append(args)
        return {"download_url": "expired" if len(requested) == 1 else "fresh"}
    monkeypatch.setattr(client, "_json_request", metadata)
    def download(value, *args):
        if value["download_url"] == "expired":
            raise FileTransferError("Object download returned HTTP 403")
        return "downloaded"
    monkeypatch.setattr(transfers, "download_object", download)
    assert client.download_file("transfer", tmp_path / "out") == "downloaded"
    assert len(requested) == 2


def test_expired_upload_url_is_refreshed(tmp_path, monkeypatch):
    import simstack.util.object_transfer as transfers
    path = tmp_path / "source"
    path.write_bytes(b"data")
    client = ObjectFileClient(server_url="https://api.test", runner_token="private")
    prepared = []
    def request(method, endpoint, payload):
        if endpoint.endswith("/prepare"):
            prepared.append(endpoint)
            return {"upload_id": "id", "upload_url": "expired" if len(prepared) == 1 else "fresh", "upload_headers": {}}
        return {"locations": [{"location_type": "s3", "object_key": "key"}]}
    monkeypatch.setattr(client, "_json_request", request)
    def upload(url, *args):
        if url == "expired":
            raise FileTransferError("Object upload returned HTTP 403")
    monkeypatch.setattr(transfers, "upload_object", upload)
    value = FileStack(name="source", locations=[FileInstance(path="source", resource="local", created_at=datetime.now())])
    assert client.publish(value, path)["object_key"] == "key"
    assert len(prepared) == 2


def test_string_constructor_get_and_pickle_work_without_initialized_context(tmp_path, monkeypatch):
    import cloudpickle
    monkeypatch.setattr("simstack.core.context.context", SimpleNamespace())
    monkeypatch.setattr(ObjectFileClient, "from_context",
        lambda **kwargs: pytest.fail("Pure constructor attempted a server request"))
    value = FileStack.from_string("import-time default", "default.txt")
    assert value.content is None and value.in_memory is False
    assert value.get(tmp_path / "first").read_text() == "import-time default"
    restored = cloudpickle.loads(cloudpickle.dumps(value))
    assert restored.get(tmp_path / "second").read_text() == "import-time default"
    assert b"import-time default" not in BSON.encode(value.model_dump_doc())


def test_multipart_streams_ranges_and_retries_only_failed_part(tmp_path, monkeypatch):
    import simstack.util.object_transfer as transfers
    path = tmp_path / "source"
    path.write_bytes(b"abcdefghijk")
    client = ObjectFileClient(server_url="https://api.test", runner_token="private")
    requests = []
    def request(method, endpoint, payload):
        requests.append(payload)
        return {"upload_url": str(payload["part_number"]), "upload_headers": {}}
    monkeypatch.setattr(client, "_json_request", request)
    uploads = []
    def upload(url, source, headers, timeout, offset, length):
        uploads.append((int(url), offset, length, source.read_bytes()[offset:offset + length]))
        if len(uploads) == 1:
            raise FileTransferError("Object upload returned HTTP 503")
    monkeypatch.setattr(transfers, "upload_object", upload)
    digest = client._publish_parts({"upload_id": "u", "multipart": {"part_size": 8, "part_count": 2}}, path)
    assert [part["part_number"] for part in requests] == [1, 1, 2]
    assert uploads == [(1, 0, 8, b"abcdefgh"), (1, 0, 8, b"abcdefgh"), (2, 8, 3, b"ijk")]
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_mutated_multipart_source_is_not_published(tmp_path, monkeypatch):
    path = tmp_path / "source"; path.write_bytes(b"original")
    client = ObjectFileClient(server_url="https://api.test", runner_token="private")
    calls = []
    def request(method, endpoint, payload):
        calls.append(endpoint)
        return {"upload_id": "u", "multipart": {"part_size": 8, "part_count": 1}}
    monkeypatch.setattr(client, "_json_request", request)
    monkeypatch.setattr(client, "_publish_parts", lambda *args: hashlib.sha256(b"changed").hexdigest())
    value = FileStack(name="source", locations=[FileInstance(path=str(path), resource="local", created_at=datetime.now())])
    with pytest.raises(FileTransferError, match="Source changed"):
        client.publish(value, path)
    assert all(not endpoint.endswith("/complete") for endpoint in calls)
