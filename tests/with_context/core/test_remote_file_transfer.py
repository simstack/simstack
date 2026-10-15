"""Active file materialization tests for API-owned execution, without SSH shims."""

from datetime import datetime
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from simstack.core.resources import allowed_resources
from simstack.core.runner_execution.state import RunnerClaimContext, activate_runner_claim, reset_runner_claim
from simstack.models.file_instance import FileInstance
from simstack.models.files import FileStack
from simstack.models.parameters import Resource
from simstack.runner_api.file_transfer import DownloadResult, FileTransferError
from simstack.runner_runtime.config import runner_task_workdir

PAYLOAD = b"checked API transfer content"


@pytest.fixture
def worker(monkeypatch, tmp_path):
    monkeypatch.setattr(allowed_resources, "_resources", ["local", "remote", "self"])
    config = SimpleNamespace(workdir=tmp_path, resource=Resource(value="local"),
                             max_inline_document_bytes=16 * 1024**2,
                             max_runner_payload_bytes=None)
    monkeypatch.setattr("simstack.core.context.context", SimpleNamespace(
        initialized=True, runner_mode=True, worker_mode=True, config=config
    ))
    claim = RunnerClaimContext(task_id="file-task", claim_id="file-claim", attempt=2)
    token = activate_runner_claim(claim)
    task_dir = runner_task_workdir(tmp_path, claim.task_id)
    task_dir.mkdir(parents=True)
    try:
        yield SimpleNamespace(config=config, claim=claim, task_dir=task_dir)
    finally:
        reset_runner_claim(token)


def test_api_worker_materializes_inline_content_in_its_task_directory(worker):
    stack = FileStack.from_string(PAYLOAD.decode(), "inline.txt")
    path = stack.get(worker.task_dir)
    assert path.read_bytes() == PAYLOAD
    assert path.parent == worker.task_dir
    assert stack.size == len(PAYLOAD)


def test_api_worker_reuses_existing_immutable_local_file(worker):
    source = worker.config.workdir / "existing.txt"
    source.write_bytes(PAYLOAD)
    stack = FileStack.from_local_file(source, in_memory=False, secure_source=True)
    assert stack.get(worker.task_dir) == source


def test_api_worker_rejects_changed_local_file_content(worker):
    source = worker.config.workdir / "existing.txt"
    source.write_bytes(PAYLOAD)
    stack = FileStack.from_local_file(source, in_memory=False, secure_source=True)
    source.write_bytes(b"different content")
    with pytest.raises(ValueError, match="immutable content metadata"):
        stack.get(worker.task_dir)


def _remote_stack():
    return FileStack(name="result.dat", in_memory=False, locations=[FileInstance(
        id="remote-instance", path="tasks/source-task/result.dat",
        resource=Resource(value="remote"), created_at=datetime.now(),
    )])


def test_api_worker_uses_claim_bound_client_and_reuses_completed_location(worker, monkeypatch):
    stack = _remote_stack()
    client = MagicMock()
    client.create_transfer.return_value = {"transfer_id": "transfer-id"}
    client.wait_until_uploaded.return_value = {"status": "uploaded"}
    client.complete_transfer.return_value = {"file_instance_id": "persisted-target-instance"}

    def download(transfer_id, target):
        target.write_bytes(PAYLOAD)
        return DownloadResult(target, len(PAYLOAD), "a" * 64)

    client.download_file.side_effect = download
    monkeypatch.setattr("simstack.models.files.ExecutionFileTransferClient.from_context", lambda: client)
    path = stack.get(worker.task_dir)
    assert path.read_bytes() == PAYLOAD
    assert client.create_transfer.call_args.kwargs == {
        "task_id": worker.claim.task_id, "claim_id": worker.claim.claim_id,
        "attempt": worker.claim.attempt, "file_stack_id": str(stack.id),
        "source_file_instance_id": "remote-instance",
        "source_resource_name": "remote", "target_resource_name": "local",
    }
    assert client.complete_transfer.call_args.kwargs["target_path"] == "tasks/file-task/result.dat"
    # Local reuse also checks the downloaded location's immutable checksum.
    import hashlib
    stack.locations[-1].checksum_sha256 = hashlib.sha256(PAYLOAD).hexdigest()
    assert stack.get(worker.task_dir) == path
    assert client.create_transfer.call_count == 1


def test_api_transfer_failure_is_explicit_and_never_invokes_old_copy_routes(worker, monkeypatch):
    stack = _remote_stack()
    client = MagicMock()
    client.create_transfer.return_value = {"transfer_id": "transfer-id"}
    client.wait_until_uploaded.side_effect = FileTransferError("source unavailable")
    monkeypatch.setattr("simstack.models.files.ExecutionFileTransferClient.from_context", lambda: client)
    with pytest.raises(FileTransferError, match="source unavailable"):
        stack.get(worker.task_dir)
    client.fail_transfer.assert_called_once()
    client.download_file.assert_not_called()
    assert not (worker.task_dir / "result.dat").exists()


def test_api_worker_does_not_return_missing_local_location(worker):
    stack = FileStack(name="missing.dat", in_memory=False, locations=[FileInstance(
        id="missing-instance", path="missing.dat", resource=Resource(value="local"),
        created_at=datetime.now(),
    )])
    with pytest.raises(ValueError, match="unavailable"):
        stack.get(worker.task_dir)


def test_inline_materialization_never_overwrites_existing_file(worker):
    target = worker.task_dir / "result.txt"
    target.write_bytes(b"existing bytes")
    stack = FileStack.from_string("new bytes", "result.txt")
    with pytest.raises(ValueError):
        stack.get(worker.task_dir)
    assert target.read_bytes() == b"existing bytes"
    assert sorted(path.name for path in worker.task_dir.iterdir()) == ["result.txt"]


@pytest.fixture
def transfer_client(worker, monkeypatch):
    client = MagicMock()
    states = {"original": {"transfer_id": "original", "status": "created"}}
    successors = {}
    checksum = hashlib.sha256(PAYLOAD).hexdigest()
    client.create_transfer.side_effect = lambda **_: dict(states["original"])
    client.get_transfer.side_effect = lambda transfer_id: dict(states[transfer_id])
    client.wait_until_uploaded.side_effect = lambda transfer_id: {
        "transfer_id": transfer_id, "status": "uploaded"
    }

    def restart(transfer_id):
        successor = successors.setdefault(transfer_id, f"successor-{len(successors)}")
        states.setdefault(successor, {"transfer_id": successor, "status": "created"})
        return dict(states[successor])

    def download(transfer_id, target):
        target.write_bytes(PAYLOAD)
        return DownloadResult(target, len(PAYLOAD), checksum)

    def complete(**payload):
        state = {**payload, "status": "completed", "file_instance_id": "persisted-target"}
        states[payload["transfer_id"]] = state
        return state

    client.restart_transfer.side_effect = restart
    client.download_file.side_effect = download
    client.complete_transfer.side_effect = complete
    monkeypatch.setattr("simstack.models.files.ExecutionFileTransferClient.from_context", lambda: client)
    return SimpleNamespace(client=client, states=states)


def test_stale_file_stack_reuses_api_completed_cache_and_restarts_missing_cache_twice(
    worker, transfer_client
):
    original = _remote_stack()
    stale = original.model_copy(deep=True)
    path = original.get(worker.task_dir)
    assert stale.get(worker.task_dir) == path
    assert transfer_client.client.download_file.call_count == 1
    transfer_client.client.restart_transfer.assert_not_called()

    for expected_download_count in (2, 3):
        path.unlink()
        assert _remote_stack().get(worker.task_dir) == path
        assert path.read_bytes() == PAYLOAD
        assert transfer_client.client.download_file.call_count == expected_download_count
    assert len(transfer_client.states) == 3
    assert transfer_client.states["original"]["status"] == "completed"


def test_completed_transfer_does_not_silently_replace_changed_cache(worker, transfer_client):
    path = _remote_stack().get(worker.task_dir)
    path.write_bytes(b"changed locally")
    with pytest.raises(FileTransferError, match="immutable metadata"):
        _remote_stack().get(worker.task_dir)
    assert path.read_bytes() == b"changed locally"
    transfer_client.client.restart_transfer.assert_not_called()


def test_completed_transfer_never_reads_neighboring_task_cache(worker, transfer_client, monkeypatch):
    _remote_stack().get(worker.task_dir)
    transfer_client.states["original"]["target_path"] = "tasks/neighbor/secret.dat"
    checksum_reader = MagicMock()
    monkeypatch.setattr("simstack.models.files.hash_file", checksum_reader)
    with pytest.raises(FileTransferError, match="active task directory"):
        _remote_stack().get(worker.task_dir)
    checksum_reader.assert_not_called()
    transfer_client.client.restart_transfer.assert_not_called()


def test_file_stack_explicitly_restarts_previous_failed_transfer(worker, transfer_client):
    transfer_client.states["original"]["status"] = "failed"
    assert _remote_stack().get(worker.task_dir).read_bytes() == PAYLOAD
    transfer_client.client.restart_transfer.assert_called_once_with("original")
    assert transfer_client.states["original"]["status"] == "failed"
