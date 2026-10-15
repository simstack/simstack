from __future__ import annotations

import asyncio
import hashlib
import json
import zlib
from threading import Event
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from simstack.core.services.file_transfer_service import FileTransferService
from simstack.models.files import FileStack
from simstack.models.file_instance import FileInstance
from simstack.models.parameters import Resource
from simstack.runner_api.file_transfer import (
    ExecutionFileTransferClient,
    FileTransferError,
    SupervisorFileTransferClient,
    path_for_file_instance,
    resolve_instance_path,
    transfer_resource_name,
)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_file_transfer_client_rejects_non_finite_or_non_positive_polling(value):
    with pytest.raises(ValueError, match="positive and finite"):
        ExecutionFileTransferClient(
            server_url="http://127.0.0.1", runner_token="execution-token",
            timeout_seconds=5, transfer_timeout_seconds=10, poll_seconds=value,
        )


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_source_service_rejects_dead_or_non_integral_concurrency(value):
    with pytest.raises(ValueError, match="max_concurrent"):
        FileTransferService(Resource(value="self"), max_concurrent=value)


def test_server_file_io_provider_cannot_replace_api_runner_boundary(monkeypatch):
    from simstack.core.file_io import current_file_io, file_io_scope
    monkeypatch.setattr("simstack.core.context.context", SimpleNamespace(initialized=True, runner_mode=True))
    with pytest.raises(RuntimeError, match="cannot replace an API runner"):
        with file_io_scope(object()):
            pytest.fail("Provider must not activate inside an API runner")
    assert current_file_io() is None


def test_source_runner_accepts_known_ui_transfer_from_exact_list_contract(
    monkeypatch,
):
    client = SupervisorFileTransferClient(
        server_url="https://simstack.example/api",
        runner_token="supervisor-token",
        timeout_seconds=60,
        transfer_timeout_seconds=3600,
        poll_seconds=5,
    )
    request = MagicMock(
        return_value={
            "transfers": [
                {
                    "transfer_id": "transfer-ui-1",
                    "status": "created",
                    "error_message": None,
                    "error_code": None,
                    "size_bytes": None,
                    "checksum_sha256": None,
                    "expires_at": "2026-08-18T12:00:00Z",
                    "file_stack_id": "file-stack-1",
                    "source_file_instance_id": "source-instance-1",
                    "source_resource_name": "remote",
                    "source_path": "outputs/result.dat",
                    "target_resource_name": "ui",
                    "request_type": "runner_to_ui",
                }
            ]
        }
    )
    monkeypatch.setattr(client._transport, "request_json", request)

    transfers = client.list_transfers()

    assert transfers[0]["request_type"] == "runner_to_ui"


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", "..\\escape"])
def test_in_memory_file_stack_name_cannot_escape_local_dir(tmp_path, name):
    local_dir = tmp_path / "target"
    stack = FileStack(
        name=name,
        in_memory=True,
        content=zlib.compress(b"payload"),
    )

    with pytest.raises(ValueError, match="strict basename"):
        stack.get_raw(Resource(value="self"), local_dir)

    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("raw_path", ["../secret", "/tmp/secret", "a/../../secret"])
def test_transfer_source_path_must_remain_inside_workdir(tmp_path, raw_path):
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(FileTransferError, match="canonical relative"):
        resolve_instance_path(raw_path, workdir)


def test_transfer_source_path_rejects_symlink_escape(tmp_path):
    workdir = tmp_path / "work"
    outside = tmp_path / "outside"
    workdir.mkdir()
    outside.mkdir()
    (workdir / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileTransferError, match="escapes runner workdir"):
        resolve_instance_path("linked/secret", workdir)


def test_file_instance_completion_path_must_remain_inside_workdir(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(FileTransferError, match="escapes runner workdir"):
        path_for_file_instance(tmp_path / "outside.dat", workdir)


def test_create_transfer_is_claim_bound_and_retry_stable(monkeypatch):
    client = ExecutionFileTransferClient(
        server_url="https://simstack.example/api",
        runner_token="worker-token",
        timeout_seconds=60,
        transfer_timeout_seconds=3600,
        poll_seconds=5,
    )
    request = MagicMock(
        return_value={
            "transfer_id": "transfer-1",
            "status": "created",
            "source_upload_url": "/api/file-transfers/transfer-1/upload",
            "target_download_url": "/api/file-transfers/transfer-1/download",
            "expires_at": "2026-08-18T12:00:00Z",
        }
    )
    monkeypatch.setattr(client._transport, "request_json", request)

    client.create_transfer(
        task_id="507f1f77bcf86cd799439011",
        claim_id="claim-1",
        attempt=3,
        file_stack_id="507f1f77bcf86cd799439012",
        source_file_instance_id="source-1",
        source_resource_name="remote",
        target_resource_name="local",
    )

    payload = request.call_args.args[2]
    semantic_payload = {
        "task_id": "507f1f77bcf86cd799439011",
        "claim_id": "claim-1",
        "attempt": 3,
        "file_stack_id": "507f1f77bcf86cd799439012",
        "source_file_instance_id": "source-1",
        "source_resource_name": "remote",
        "target_resource_name": "local",
        "request_type": "runner_to_runner",
    }
    expected_request_id = hashlib.sha256(
        json.dumps(semantic_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert payload == {
        "task_id": "507f1f77bcf86cd799439011",
        "claim_id": "claim-1",
        "attempt": 3,
        "request_id": expected_request_id,
        "file_stack_id": "507f1f77bcf86cd799439012",
        "source_file_instance_id": "source-1",
        "source_resource_name": "remote",
        "target_resource_name": "local",
        "request_type": "runner_to_runner",
    }


@pytest.mark.parametrize(
    "upload_error",
    [
        FileTransferError("temporary server error", status_code=503),
        FileTransferError("transport retry exhausted"),
        OSError("connection reset"),
    ],
)
def test_source_upload_transient_failure_remains_recoverable(
    monkeypatch, tmp_path, upload_error
):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "source.dat").write_bytes(b"payload")
    monkeypatch.setattr(
        "simstack.core.services.file_transfer_service.context",
        SimpleNamespace(config=SimpleNamespace(workdir=workdir)),
    )
    client = MagicMock()
    client.upload_file.side_effect = upload_error
    service = FileTransferService(Resource(value="local"))

    assert (
        service._upload_transfer(
            client,
            {"transfer_id": "transfer-1", "source_path": "source.dat"},
        )
        is False
    )

    client.fail_transfer.assert_not_called()


def test_source_upload_deterministic_http_failure_is_terminal(monkeypatch, tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "source.dat").write_bytes(b"payload")
    monkeypatch.setattr(
        "simstack.core.services.file_transfer_service.context",
        SimpleNamespace(config=SimpleNamespace(workdir=workdir)),
    )
    client = MagicMock()
    client.upload_file.side_effect = FileTransferError(
        "request rejected", status_code=422
    )
    service = FileTransferService(Resource(value="local"))

    assert (
        service._upload_transfer(
            client,
            {"transfer_id": "transfer-1", "source_path": "source.dat"},
        )
        is False
    )

    client.fail_transfer.assert_called_once_with(
        "transfer-1",
        error_message="request rejected",
        error_code="SOURCE_UPLOAD_FAILED",
    )


def test_non_runner_domain_file_instance_without_id_does_not_generate_identity():
    payload = {
        "path": "remote/task/output.dat",
        "resource": {"value": "remote"},
        "created_at": datetime(2026, 8, 17, 12, 0, 0),
    }

    first = FileInstance.model_validate(payload)
    restored = FileInstance.model_validate(first.model_dump(mode="python"))

    assert first.id is None
    assert restored.id is None


def test_new_local_file_instance_gets_id_only_on_creation_path(monkeypatch, tmp_path):
    source = tmp_path / "output.dat"
    source.write_bytes(b"payload")
    monkeypatch.setattr(
        "simstack.core.context.context",
        SimpleNamespace(
            config=SimpleNamespace(
                workdir=tmp_path,
                resource=Resource(value="local"),
            )
        ),
    )

    instance = FileInstance.from_local_file(
        source,
        file_stack_id="file-stack-id",
    )

    assert isinstance(instance.id, str)
    assert instance.id


@pytest.mark.parametrize("invalid_id", ["", " ", " persisted-id "])
def test_file_instance_rejects_noncanonical_persisted_id(invalid_id):
    with pytest.raises(ValueError, match="opaque non-empty"):
        FileInstance(
            id=invalid_id,
            path="remote/task/output.dat",
            resource=Resource(value="remote"),
            created_at=datetime(2026, 8, 17, 12, 0, 0),
        )


def test_legacy_remote_location_without_id_fails_closed_before_transfer(
    monkeypatch, tmp_path
):
    remote = FileInstance(
        path="remote/task/output.dat",
        resource=Resource(value="remote"),
        created_at=datetime(2026, 8, 17, 12, 0, 0),
    )
    stack = FileStack(name="output.dat", in_memory=False, locations=[remote])
    create_transfer = MagicMock()
    monkeypatch.setattr(
        "simstack.models.files.ExecutionFileTransferClient.from_context",
        lambda: SimpleNamespace(create_transfer=create_transfer),
    )
    monkeypatch.setattr(
        "simstack.core.runner_execution.current_runner_claim",
        lambda: SimpleNamespace(task_id="task-1", claim_id="claim-1", attempt=1),
    )
    monkeypatch.setattr(
        "simstack.core.context.context",
        SimpleNamespace(config=SimpleNamespace(workdir=tmp_path)),
    )

    with pytest.raises(ValueError, match="persisted source FileInstance id"):
        stack._get_via_server_transfer(
            remote,
            Resource(value="local"),
            tmp_path,
        )

    create_transfer.assert_not_called()


def test_file_transfer_has_no_environment_or_jwt_context_fallback(
    monkeypatch,
):
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "unverified.jwt.value")
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://simstack.example/api")
    monkeypatch.setattr(
        "simstack.core.context.context", SimpleNamespace(runner_mode=False)
    )

    with pytest.raises(FileTransferError, match="API-only runner mode"):
        ExecutionFileTransferClient.from_context()
    with pytest.raises(FileTransferError, match="outside API-only runner mode"):
        transfer_resource_name(Resource(value="self"))


def test_external_file_instance_persists_the_copied_file_not_its_directory(tmp_path):
    source = tmp_path / "external" / "result.dat"
    source.parent.mkdir()
    source.write_bytes(b"external source")
    workdir = tmp_path / "work"
    workdir.mkdir()

    instance = FileInstance.from_local_file(
        source,
        file_stack_id="stack-id",
        make_copy=False,
        workdir=workdir,
        resource=Resource(value="local"),
    )

    stored_file = resolve_instance_path(instance.path, workdir)
    assert stored_file.is_file()
    assert stored_file.read_bytes() == b"external source"
    assert stored_file != source
    assert instance.checksum_sha256 == hashlib.sha256(b"external source").hexdigest()


def test_make_copy_protects_a_file_already_in_the_workdir(tmp_path):
    source = tmp_path / "result.dat"
    source.write_bytes(b"immutable content")
    instance = FileInstance.from_local_file(
        source,
        file_stack_id="stack-id",
        make_copy=True,
        workdir=tmp_path,
        resource=Resource(value="local"),
    )
    source.write_bytes(b"changed source")

    assert resolve_instance_path(instance.path, tmp_path).read_bytes() == b"immutable content"


def test_file_backed_factory_needs_no_global_context_when_resource_is_explicit(tmp_path):
    source = tmp_path / "payload.dat"
    payload = bytes(range(256)) * 100
    source.write_bytes(payload)
    stack = FileStack.from_local_file(
        source,
        max_inline_document_bytes=256,
        workdir=tmp_path,
        resource=Resource(value="local"),
    )

    assert stack.in_memory is False
    assert stack.content is None
    assert resolve_instance_path(stack.locations[0].path, tmp_path).read_bytes() == payload


def test_inline_factory_measures_metadata_not_just_compressed_content(tmp_path):
    source = tmp_path / "tiny.dat"
    source.write_bytes(b"x")
    stack = FileStack.from_local_file(
        source,
        max_inline_document_bytes=100,
        workdir=tmp_path,
        resource=Resource(value="local"),
    )

    assert len(zlib.compress(b"x")) < 100
    assert stack.in_memory is False
    assert stack.locations[0].size_bytes == 1


def test_large_runner_string_uses_file_backed_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "simstack.core.context.context",
        SimpleNamespace(
            initialized=True,
            runner_mode=True,
            worker_mode=True,
            config=SimpleNamespace(
                workdir=tmp_path,
                resource=Resource(value="local"),
                max_inline_document_bytes=100,
            ),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.runner_execution.current_runner_claim",
        lambda: SimpleNamespace(task_id="active-task"),
    )

    stack = FileStack.from_string("payload", "result.txt")

    assert stack.in_memory is False
    assert resolve_instance_path(stack.locations[0].path, tmp_path).read_text() == "payload"
    assert stack.locations[0].path.startswith("tasks/active-task/")


def test_explicit_request_policy_is_measured_on_full_file_wire_document(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "simstack.core.context.context",
        SimpleNamespace(
            initialized=True,
            runner_mode=True,
            worker_mode=True,
            config=SimpleNamespace(
                workdir=tmp_path,
                resource=Resource(value="local"),
                max_inline_document_bytes=16 * 1024**2,
                max_runner_payload_bytes=100,
            ),
        ),
    )
    source = tmp_path / "tiny.dat"
    source.write_bytes(b"x")

    stack = FileStack.from_local_file(source)

    assert len(zlib.compress(b"x")) < 100
    assert stack.in_memory is False
    assert resolve_instance_path(stack.locations[0].path, tmp_path).read_bytes() == b"x"


@pytest.mark.parametrize("source_path", ["../escape.dat", "missing.dat"])
def test_unusable_source_upload_is_failed_instead_of_polling_forever(
    monkeypatch, tmp_path, source_path
):
    monkeypatch.setattr(
        "simstack.core.services.file_transfer_service.context",
        SimpleNamespace(config=SimpleNamespace(workdir=tmp_path)),
    )
    client = MagicMock()
    service = FileTransferService(Resource(value="local"))

    assert service._upload_transfer(
        client, {"transfer_id": "transfer-1", "source_path": source_path}
    ) is False

    client.upload_file.assert_not_called()
    assert client.fail_transfer.call_args.kwargs["error_code"] == "SOURCE_FILE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_source_service_stop_drains_upload_thread_and_cancels_queued_work(monkeypatch, tmp_path):
    source = tmp_path / "source.dat"
    source.write_bytes(b"immutable content")
    monkeypatch.setattr(
        "simstack.core.services.file_transfer_service.context",
        SimpleNamespace(config=SimpleNamespace(workdir=tmp_path)),
    )
    entered = Event()
    exited = Event()
    client = MagicMock()
    client.list_transfers.return_value = [
        {"transfer_id": name, "source_path": "source.dat"} for name in ("active", "queued")
    ]

    def upload(transfer_id, path, *, stop_event):
        entered.set()
        try:
            assert stop_event.wait(2), "stop must signal the running upload thread"
            raise FileTransferError("File transfer upload stopped for runner shutdown")
        finally:
            exited.set()

    client.upload_file.side_effect = upload
    service = FileTransferService(Resource(value="local"), max_concurrent=1)
    service._client = client
    await service.execute()
    assert await asyncio.to_thread(entered.wait, 2)
    await service.stop()
    assert exited.is_set()
    assert not service._running_tasks
    assert not service._active_tasks
    assert client.upload_file.call_count == 1
    client.fail_transfer.assert_not_called()
    await service.execute()
    assert client.list_transfers.call_count == 1
