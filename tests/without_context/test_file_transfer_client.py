import hashlib
import json
from pathlib import Path

import pytest

from simstack.util.file_transfer_client import FileTransferClient, FileTransferError


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


def _client_with_responses(responses: list[FakeResponse], recorder: list[dict]) -> FileTransferClient:
    client = FileTransferClient(
        server_url="https://simstack.example.org",
        runner_token="token",
    )

    def fake_connection():
        return FakeConnection(responses, recorder)

    client._connection = fake_connection  # type: ignore[method-assign]
    return client


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
    client = FileTransferClient(
        server_url="https://simstack.example.org",
        runner_token="token",
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
