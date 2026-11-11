from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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
    }

    assert service._upload_transfer(client, transfer) is False

    client.fail_transfer.assert_called_once_with(
        "6ad029a0cc353f863203af1c",
        error_message=str(client.upload_file.side_effect),
        error_code="SOURCE_NOT_READABLE",
    )
