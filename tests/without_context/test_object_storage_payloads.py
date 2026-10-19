"""Object payload format, reference and claim-bound HTTP regression evidence."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import unquote, urlparse, parse_qs

import numpy as np
import pandas as pd
import pytest

from simstack.core.runner_execution.blobs import publish_runtime_blobs
from simstack.core.runner_execution.state import RunnerClaimContext, activate_runner_claim, reset_runner_claim
from simstack.models.array_storage import ArrayStorage
from simstack.models.document import REFERENCE_SCHEMA_KEY
from simstack.models.files import BlobReference, FileStack
from simstack.models.images2d import Image2DArtifactModel
from simstack.models.pandas_model import PandasModel
from simstack.models.parameters import Resource
from simstack.runner_api.file_transfer import ExecutionFileTransferClient, FileTransferError
from simstack.runner_protocol import FILE_UPLOAD_SCOPE, RUNNER_EXECUTION_SCOPES


def test_execution_credential_can_publish_claim_bound_outputs():
    assert FILE_UPLOAD_SCOPE in RUNNER_EXECUTION_SCOPES


@pytest.mark.parametrize("array", [
    np.array([0, 2**64 - 1], dtype=np.uint64),
    np.array([-3, 5], dtype=np.int16),
    np.array([np.nan, np.inf, -np.inf, 1.25], dtype=np.float32),
    np.empty((2, 0, 3), dtype=np.float64),
    np.arange(60, dtype=np.int32).reshape(5, 12)[:, ::3],
    np.array(True),
])
def test_array_externalization_preserves_dtype_shape_values(array):
    model = ArrayStorage(name="array")
    model.array = array
    model.prepare_blob_payload()
    assert model.data_json is None
    assert model.payload_file.name == "array.npy"
    assert model.payload_format == "npy"
    assert model.array.dtype == array.dtype
    np.testing.assert_array_equal(model.array, array)
    assert model.model_dump_doc()["payload_file"] == str(model.payload_file.id)
    assert ArrayStorage.model_fields["payload_file"].json_schema_extra[REFERENCE_SCHEMA_KEY]


def test_array_legacy_json_is_explicitly_readable_and_object_dtype_rejected():
    legacy = ArrayStorage(name="legacy", shape="2,2", data_json="[1,2,3,4]")
    np.testing.assert_array_equal(legacy.array, [[1, 2], [3, 4]])
    with pytest.raises(ValueError, match="object dtype"):
        legacy.array = np.array([object()], dtype=object)


def test_array_payload_metadata_mismatch_is_rejected():
    model = ArrayStorage(name="array")
    model.array = np.array([1, 2], dtype=np.int16)
    model.prepare_blob_payload()
    model.dtype = "<f4"
    with pytest.raises(ValueError, match="shape/dtype"):
        _ = model.array


def test_parquet_preserves_supported_types_and_legacy_pickle_reads():
    table = pd.DataFrame({
        "integer": pd.array([1, None], dtype="Int64"),
        "category": pd.Categorical(["a", "b"]),
        "value": [1.5, float("nan")],
        "time": pd.date_range("2026-01-01", periods=2, tz="UTC"),
    }, index=pd.Index(["x", "y"], name="row"))
    model = PandasModel.from_data_frame(table)
    assert model.content_
    pd.testing.assert_frame_equal(model.table, table)
    model.prepare_blob_payload()
    assert model.content_ == b""
    assert model.payload_format == "parquet"
    assert model.file_stack.name == "table.parquet"
    pd.testing.assert_frame_equal(model.table, table)
    assert model.model_dump_doc()["file_stack"] == str(model.file_stack.id)
    assert PandasModel.model_fields["file_stack"].json_schema_extra[REFERENCE_SCHEMA_KEY]


def test_parquet_unsupported_objects_fail_without_pickle_fallback():
    model = PandasModel.from_data_frame(pd.DataFrame({"objects": [object()]}))
    with pytest.raises(ValueError, match="unsupported by Parquet"):
        model.prepare_blob_payload()
    assert model.file_stack is None
    assert model.content_


def test_image_externalization_retains_access_and_reference():
    model = Image2DArtifactModel(name="figure", format="png", data=b"pixels")
    model.prepare_blob_payload()
    assert model.inline_data == b""
    assert model.data == b"pixels"
    assert model.get_bytes() == b"pixels"
    assert model.get_data_uri() == "data:image/png;base64,cGl4ZWxz"
    assert model.model_dump_doc()["payload_file"] == str(model.payload_file.id)
    assert model.model_dump_doc()["data"] == {"$binary": {"base64": "", "subType": "00"}}
    assert "data" in model.model_json_schema()["properties"]
    assert "inline_data" not in model.model_json_schema()["properties"]
    model.data = b"replacement"
    assert model.payload_file is None and model.data == b"replacement"


def test_image_payload_schema_uses_generic_output_file_field():
    from simstack.util.generate_ui_schema import resolve_ui_schema

    assert resolve_ui_schema(Image2DArtifactModel)["payload_file"] == {
        "ui:field": "FileField", "ui:options": {"isInput": False},
    }
    assert "data" in Image2DArtifactModel.model_json_schema()["properties"]


@pytest.fixture
def blob_http():
    stored = {}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_PUT(self):
            path = urlparse(self.path)
            calls.append(("PUT", self.path, dict(self.headers)))
            assert self.headers["Authorization"] == "Bearer execution-token"
            assert parse_qs(path.query) == {"claim_id": ["claim 1"], "attempt": ["2"]}
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            assert len(raw) == int(self.headers["X-File-Size"])
            assert hashlib.sha256(raw).hexdigest() == self.headers["X-Checksum-SHA256"]
            identifier = path.path.split("/")[-2]
            stack = FileStack(id=identifier, name=unquote(self.headers["X-File-Name"]),
                              size=len(raw), is_hashable=True,
                              hash=hashlib.sha256(raw).hexdigest(),
                              blob=BlobReference(id=identifier, size_bytes=len(raw),
                                                 checksum_sha256=hashlib.sha256(raw).hexdigest()))
            stored[identifier] = raw
            document = stack.model_dump_doc()
            document["id"] = document.pop("_id")
            payload = json.dumps(document).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            calls.append(("GET", self.path, dict(self.headers)))
            assert self.headers["Authorization"] == "Bearer execution-token"
            raw = stored[urlparse(self.path).path.split("/")[-2]]
            self.send_response(200)
            self.send_header("X-File-Size", str(len(raw)))
            self.send_header("X-Checksum-SHA256", hashlib.sha256(raw).hexdigest())
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ExecutionFileTransferClient(
        server_url=f"http://127.0.0.1:{server.server_port}/api",
        runner_token="execution-token", timeout_seconds=5,
        transfer_timeout_seconds=10, poll_seconds=0.01, retry_attempts=0,
    )
    try:
        yield client, stored, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_claim_bound_publish_read_and_local_cache(blob_http, monkeypatch, tmp_path):
    client, stored, calls = blob_http
    fake_context = SimpleNamespace(initialized=True, runner_mode=True, worker_mode=True,
                                   config=SimpleNamespace(object_storage=True, workdir=tmp_path,
                                                          resource=Resource(value="local"),
                                                          max_inline_document_bytes=1_000_000))
    monkeypatch.setattr("simstack.core.context.context", fake_context)
    monkeypatch.setattr("simstack.core.runner_execution.blobs.context", fake_context)
    monkeypatch.setattr(ExecutionFileTransferClient, "from_context", classmethod(lambda cls: client))
    claim_token = activate_runner_claim(RunnerClaimContext(task_id="task-1", claim_id="claim 1", attempt=2))
    try:
        stack = FileStack.from_bytes(b"content" * 10000, "данные.txt")
        identity = stack.id
        publish_runtime_blobs([stack])
        assert stack.id == identity
        assert stack.blob.id == str(identity)
        assert stack.content is None and stack.locations == [] and not stack.in_memory
        assert stored[str(identity)] == b"content" * 10000
        publish_runtime_blobs([stack])
        assert len(calls) == 1  # Blob references are reused, never reuploaded.
        destination = tmp_path / "tasks" / "task-1" / "input"
        path = stack.get(destination)
        assert path.read_bytes() == stored[str(identity)]
        assert stack.get(destination) == path
        assert len(calls) == 2  # Verified local file is a reusable cache.
        path.write_bytes(b"corruption")
        with pytest.raises(FileTransferError, match="cache"):
            stack.get(destination)
        assert calls[0][1].startswith(f"/api/runner/tasks/task-1/files/{identity}/content?")
        assert calls[0][2]["X-File-Name"] == "%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D0%B5.txt"
    finally:
        reset_runner_claim(claim_token)


def test_failed_upload_preserves_unpublished_bytes(monkeypatch, tmp_path):
    fake_context = SimpleNamespace(initialized=True, config=SimpleNamespace(object_storage=True, workdir=tmp_path))
    monkeypatch.setattr("simstack.core.runner_execution.blobs.context", fake_context)
    monkeypatch.setattr("simstack.core.context.context", SimpleNamespace(initialized=False, runner_mode=False,
                                                                       config=SimpleNamespace(resource=Resource(value="server"))))
    def fail(**kwargs):
        raise FileTransferError("storage unavailable")
    monkeypatch.setattr(ExecutionFileTransferClient, "from_context", classmethod(lambda cls: SimpleNamespace(publish_blob=fail)))
    token = activate_runner_claim(RunnerClaimContext(task_id="task-1", claim_id="claim 1", attempt=2))
    try:
        stack = FileStack.from_bytes(b"important", "output")
        with pytest.raises(FileTransferError, match="storage unavailable"):
            publish_runtime_blobs([stack])
        assert stack.blob is None
        assert stack.read_bytes() == b"important"
    finally:
        reset_runner_claim(token)


def test_declared_table_and_info_file_upload_before_result_serialization(blob_http, monkeypatch, tmp_path):
    from simstack.models.simple_table import SimpleTable
    from simstack.models.payloads import read_table_rows
    client, stored, calls = blob_http
    fake_context = SimpleNamespace(initialized=True, runner_mode=True, worker_mode=True,
                                   config=SimpleNamespace(object_storage=True, workdir=tmp_path,
                                                          resource=Resource(value="local"),
                                                          max_inline_document_bytes=1_000_000))
    monkeypatch.setattr("simstack.core.context.context", fake_context)
    monkeypatch.setattr("simstack.core.runner_execution.blobs.context", fake_context)
    monkeypatch.setattr(ExecutionFileTransferClient, "from_context", classmethod(lambda cls: client))
    token = activate_runner_claim(RunnerClaimContext(task_id="task-1", claim_id="claim 1", attempt=2))
    try:
        table = SimpleTable(row=[{"i": i} for i in range(45)])
        info = FileStack.from_bytes(b"full log", "info.txt")
        publish_runtime_blobs([table, info])
        assert len(calls) == 2
        assert table.model_dump_doc()["row"] == []
        assert table.payload.file.blob and info.blob
        assert read_table_rows(stored[str(table.payload.file.id)], 40, 5) == [{"i": i} for i in range(40, 45)]
        previous = table.payload.file.id
        publish_runtime_blobs([table])
        assert table.payload.file.id == previous and len(calls) == 2
    finally:
        reset_runner_claim(token)
