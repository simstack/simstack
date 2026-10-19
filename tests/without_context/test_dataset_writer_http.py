"""Actual local HTTP uploads replay header+disk spool with bounded reads."""
from __future__ import annotations

import hashlib
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from simstack.core.runner_execution.state import RunnerClaimContext, activate_runner_claim, reset_runner_claim
from simstack.runner_api.datasets import DatasetRunnerClient, MAX_DATASET_PAGE_BYTES, _PrefixedBody


@pytest.mark.parametrize("failure", ["503", "lost-200"])
def test_actual_http_snapshot_prefix_and_spool_replay_identical_after_retry(tmp_path, monkeypatch, failure):
    calls, reads = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            remaining = int(self.headers["Content-Length"])
            digest = hashlib.sha256()
            while remaining:
                block = self.rfile.read(min(4096, remaining))
                assert block
                digest.update(block)
                remaining -= len(block)
            calls.append((self.path, dict(self.headers), digest.hexdigest()))
            raw = b'{"okay":true}'
            self.send_response(503 if failure == "503" and len(calls) == 1 else 200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    transport = SimpleNamespace(connection=lambda: http.client.HTTPConnection(*server.server_address, timeout=3),
                                path=lambda path: "/api/runner" + path)
    api = SimpleNamespace(_transport=transport, runner_token="test-execution", retry_attempts=1, retry_backoff_seconds=0)
    client = DatasetRunnerClient(api)
    original_read = _PrefixedBody.read

    def bounded_read(self, size):
        assert 0 < size <= 64 * 1024
        reads.append(size)
        return original_read(self, size)

    monkeypatch.setattr(_PrefixedBody, "read", bounded_read)
    if failure == "lost-200":
        original_once = client._request_once

        def request_once(*args, **kwargs):
            result = original_once(*args, **kwargs)
            if len(calls) == 1:
                raise OSError("Response lost after server accepted snapshot")
            return result

        monkeypatch.setattr(client, "_request_once", request_once)
    source = tmp_path / "rows.ndjson"
    source.write_bytes((b'{"payload":"' + b"x" * 1024 + b'"}\n') * 512)
    header = {"version": 1, "section_id": "1" * 24, "model_types": {"число": "IntData"},
              "column_defs": [], "expected_revision": None}
    prefix = json.dumps(header, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    expected = hashlib.sha256(prefix + source.read_bytes()).hexdigest()
    token = activate_runner_claim(RunnerClaimContext(task_id="task", claim_id="claim", attempt=2))
    try:
        assert client.snapshot("2" * 24, "section/with space", kind="dict", source=source, header=header) == {"okay": True}
    finally:
        reset_runner_claim(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert len(calls) == 2 and calls[0] == calls[1]
    path, headers, checksum = calls[0]
    assert "section%2Fwith%20space/snapshot" in path
    assert "claim_id=claim" in path and "attempt=2" in path and "kind=dict" in path
    assert headers["Authorization"] == "Bearer test-execution"
    assert headers["Content-Type"] == "application/x-ndjson"
    assert int(headers["Content-Length"]) == len(prefix) + source.stat().st_size
    assert checksum == expected and len(reads) > 100
    assert list(tmp_path.iterdir()) == [source]  # No second full NDJSON copy.


def test_snapshot_header_budget_rejects_before_network_or_claim_access(tmp_path):
    source = tmp_path / "empty.ndjson"
    source.touch()
    with pytest.raises(ValueError, match="header exceeds"):
        DatasetRunnerClient(None).snapshot("1" * 24, "rows", kind="dict", source=source,
                                          header={"oversized": "x" * MAX_DATASET_PAGE_BYTES})
