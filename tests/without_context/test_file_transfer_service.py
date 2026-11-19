from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from simstack.models.file_instance import FileInstance
from simstack.models.files import FileStack
from simstack.util.file_transfer_client import FileTransferClient, FileTransferError
from simstack.core.services.file_transfer_service import FileTransferService
from simstack.models.parameters import Resource


def test_permission_error_fails_transfer(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "simstack.core.services.file_transfer_service.context",
        SimpleNamespace(config=SimpleNamespace(workdir=tmp_path)),
    )
    service = FileTransferService(Resource(value="big2"), interval=10)
    client = Mock()
    client.upload_file.side_effect = PermissionError(13, "Permission denied")
    transfer = {
        "transfer_id": "6ad029a0cc353f863203af1c",
        "source_path": "pyscf_hessian/task/result.wfn.npy",
        "file_stack_id": "6ad029a0cc353f863203af1d",
        "source_resource_name": "big2",
    }

    assert service._upload_transfer(client, transfer) is False

    client.fail_transfer.assert_called_once_with(
        "6ad029a0cc353f863203af1c",
        error_message=str(client.upload_file.side_effect),
        error_code="SOURCE_NOT_READABLE",
    )

@pytest.mark.parametrize("source_failed", [True, False], ids=["source-failed", "timeout"])
def test_target_reports_only_upload_timeout(monkeypatch, tmp_path, source_failed):
    client = FileTransferClient(server_url="http://localhost", runner_token="unused")
    monkeypatch.setattr(FileTransferClient, "from_context", lambda **kwargs: client)
    monkeypatch.setattr(client, "create_transfer", lambda **kwargs: {"transfer_id": "transfer"})
    monkeypatch.setattr(client, "get_transfer", lambda _: {"status": "failed", "error_message": "file missing"})
    report_failure = Mock()
    monkeypatch.setattr(client, "fail_transfer", report_failure)
    monkeypatch.setenv("SIMSTACK_FILE_TRANSFER_TIMEOUT_SECONDS", "10" if source_failed else "0")
    source = FileInstance(path="input.bin", resource="A", created_at=datetime.now())
    transfer = FileStack(name="input.bin", locations=[source])
    with pytest.raises(FileTransferError, match="file missing" if source_failed else "Timed out"):
        transfer._get_via_server_transfer(source, Resource(value="B"), tmp_path)
    if source_failed:
        report_failure.assert_not_called()
    else:
        assert report_failure.call_args.kwargs["error_code"] == "SOURCE_RUNNER_UNAVAILABLE"
