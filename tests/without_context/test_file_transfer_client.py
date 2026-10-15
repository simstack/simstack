import hashlib
import io
import json
from pathlib import Path
from threading import Event

import pytest

from simstack.runner_api.file_transfer.transport import FileTransferTransport
from simstack.runner_api.file_transfer import FileTransferError


class FakeResponse:
    def __init__(self, status: int, payload: dict | str):
        self.status = status
        self.reason = "OK" if status < 400 else "Error"
        if isinstance(payload, dict):
            self._body = json.dumps(payload).encode()
            self._content_type = "application/json"
        else:
            self._body = payload.encode("utf-8")
            self._content_type = "text/html"

    def read(self):
        return self._body

    def getheader(self, name):
        if name == "Content-Type":
            return self._content_type
        return None


class FakeConnection:
    def __init__(self, responses: list[FakeResponse], recorder: list[dict]):
        self._responses = responses
        self._recorder = recorder
        self._headers: dict[str, str] = {}
        self._body = b""
        self._method = None
        self._path = None

    def putrequest(self, method, path):
        self._method = method
        self._path = path
        self._headers = {}
        self._body = b""

    def putheader(self, key, value):
        self._headers[key] = value

    def endheaders(self):
        pass

    def send(self, data):
        self._body += data

    def getresponse(self):
        self._recorder.append(
            {
                "method": self._method,
                "path": self._path,
                "headers": dict(self._headers),
                "body": self._body,
            }
        )
        return self._responses.pop(0)

    def close(self):
        pass


def _client_with_responses(responses: list[FakeResponse], recorder: list[dict]) -> FileTransferTransport:
    client = FileTransferTransport(
        server_url="https://simstack.example.org",
        token_provider=lambda: "token",
        timeout_seconds=30,
        retry_attempts=0,
        retry_backoff_seconds=0,
    )

    def fake_connection():
        return FakeConnection(responses, recorder)

    client._connection = fake_connection  # type: ignore[method-assign]
    return client


def test_upload_shutdown_closes_inflight_connection_without_retrying_or_publishing(tmp_path, monkeypatch):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"immutable bytes")
    stop_event = Event()
    connections = []

    class InterruptedConnection(FakeConnection):
        def __init__(self):
            super().__init__([], [])
            self.closed = False

        def send(self, data):
            super().send(data)
            stop_event.set()

        def close(self):
            self.closed = True

    client = _client_with_responses([], [])
    client.retry_attempts = 2
    client.chunk_size = 2
    monkeypatch.setenv("SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES", "100")

    def connection():
        result = InterruptedConnection()
        connections.append(result)
        return result

    monkeypatch.setattr(client, "_connection", connection)
    with pytest.raises(FileTransferError, match="runner shutdown"):
        client.upload_file("upload", source, stop_event=stop_event)
    assert len(connections) == 1
    assert connections[0].closed
    assert connections[0]._body == b"im"


def test_upload_file_small_payload_omits_content_range(tmp_path: Path):
    payload = b"hi"
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    checksum = hashlib.sha256(payload).hexdigest()
    recorder: list[dict] = []
    client = _client_with_responses(
        [
            FakeResponse(
                200,
                {
                    "status": "uploaded",
                    "size_bytes": len(payload),
                    "checksum_sha256": checksum,
                },
            )
        ],
        recorder,
    )

    result = client.upload_file("abc", source)

    assert result["status"] == "uploaded"
    assert len(recorder) == 1
    assert "Content-Range" not in recorder[0]["headers"]
    assert recorder[0]["body"] == payload
    assert recorder[0]["headers"]["Content-Length"] == str(len(payload))


def test_upload_file_sends_content_range_chunks(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES", "8")
    payload = b"abcdefghijklmnop"
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    checksum = hashlib.sha256(payload).hexdigest()
    recorder: list[dict] = []
    client = _client_with_responses(
        [
            FakeResponse(
                200,
                {"status": "source_uploading", "size_bytes": 8, "checksum_sha256": ""},
            ),
            FakeResponse(
                200,
                {
                    "status": "uploaded",
                    "size_bytes": len(payload),
                    "checksum_sha256": checksum,
                },
            ),
        ],
        recorder,
    )

    result = client.upload_file("abc", source)

    assert result["status"] == "uploaded"
    assert [item["headers"]["Content-Range"] for item in recorder] == [
        "bytes 0-7/16",
        "bytes 8-15/16",
    ]
    assert recorder[0]["body"] == payload[:8]
    assert recorder[1]["body"] == payload[8:]


def test_upload_file_retries_413_by_halving_chunk_size(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES", "16")
    payload = b"x" * 32
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    checksum = hashlib.sha256(payload).hexdigest()
    html_413 = (
        "<html><head><title>413 Request Entity Too Large</title></head></html>"
    )
    recorder: list[dict] = []
    client = _client_with_responses(
        [
            FakeResponse(413, html_413),
            FakeResponse(
                200,
                {"status": "source_uploading", "size_bytes": 8, "checksum_sha256": ""},
            ),
            FakeResponse(
                200,
                {"status": "source_uploading", "size_bytes": 16, "checksum_sha256": ""},
            ),
            FakeResponse(
                200,
                {"status": "source_uploading", "size_bytes": 24, "checksum_sha256": ""},
            ),
            FakeResponse(
                200,
                {
                    "status": "uploaded",
                    "size_bytes": 32,
                    "checksum_sha256": checksum,
                },
            ),
        ],
        recorder,
    )

    result = client.upload_file("abc", source)

    assert result["status"] == "uploaded"
    assert recorder[0]["headers"]["Content-Range"] == "bytes 0-15/32"
    assert [item["headers"]["Content-Range"] for item in recorder[1:]] == [
        "bytes 0-7/32",
        "bytes 8-15/32",
        "bytes 16-23/32",
        "bytes 24-31/32",
    ]


def test_upload_file_rejects_invalid_chunk_size(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES", "nope")
    source = tmp_path / "payload.bin"
    source.write_bytes(b"hi")
    client = FileTransferTransport(
        server_url="https://simstack.example.org",
        token_provider=lambda: "token",
        timeout_seconds=30,
        retry_attempts=0,
        retry_backoff_seconds=0,
    )
    with pytest.raises(ValueError, match="must be an integer"):
        client.upload_file("abc", source)


def test_upload_file_raises_after_413_when_chunks_cannot_shrink(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES", "256")
    payload = b"y" * 300
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    html_413 = "<html><head><title>413 Request Entity Too Large</title></head></html>"
    recorder: list[dict] = []
    client = _client_with_responses(
        [
            FakeResponse(413, html_413),
            FakeResponse(413, html_413),
        ],
        recorder,
    )

    with pytest.raises(FileTransferError, match="HTTP 413"):
        client.upload_file("abc", source)


class StreamingResponse:
    status = 200
    reason = "OK"

    def __init__(self, payload: bytes, *, size: int | None, checksum: str | None):
        self._body = io.BytesIO(payload)
        self._headers = {"X-File-Size": str(size) if size is not None else None,
                         "X-Checksum-SHA256": checksum}

    def read(self, size=-1):
        return self._body.read(size)

    def getheader(self, name):
        return self._headers.get(name)


class DownloadConnection:
    def __init__(self, response):
        self.response = response

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        pass


def _download_transport(response):
    client = FileTransferTransport(
        server_url="https://simstack.example",
        token_provider=lambda: "token",
        timeout_seconds=10,
        retry_attempts=0,
        retry_backoff_seconds=0,
    )
    client._connection = lambda: DownloadConnection(response)
    return client


def test_download_is_integrity_checked_before_atomic_publication(tmp_path):
    payload = b"checked bytes"
    checksum = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "result.dat"
    client = _download_transport(StreamingResponse(payload, size=len(payload), checksum=checksum))

    result = client.download_file("/download", target)

    assert result.path == target
    assert target.read_bytes() == payload
    assert result.checksum_sha256 == checksum
    assert sorted(path.name for path in tmp_path.iterdir()) == ["result.dat"]


@pytest.mark.parametrize(
    ("payload", "size", "checksum"),
    [
        (b"truncated", 100, hashlib.sha256(b"truncated").hexdigest()),
        (b"too much", 2, hashlib.sha256(b"too much").hexdigest()),
        (b"changed", 7, "0" * 64),
        (b"missing metadata", None, None),
    ],
)
def test_invalid_download_never_publishes_partial_or_corrupt_file(
    tmp_path, payload, size, checksum
):
    target = tmp_path / "result.dat"
    client = _download_transport(StreamingResponse(payload, size=size, checksum=checksum))

    with pytest.raises(FileTransferError):
        client.download_file("/download", target)

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_never_overwrites_a_concurrent_file(tmp_path):
    payload = b"new bytes"
    target = tmp_path / "result.dat"
    target.write_bytes(b"existing bytes")
    client = _download_transport(StreamingResponse(
        payload, size=len(payload), checksum=hashlib.sha256(payload).hexdigest()
    ))

    with pytest.raises(FileExistsError):
        client.download_file("/download", target)

    assert target.read_bytes() == b"existing bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["result.dat"]
