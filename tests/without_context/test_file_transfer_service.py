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

    client.fail_transfer.assert_called_once()
    assert client.fail_transfer.call_args.kwargs["error_code"] == "SOURCE_NOT_READABLE"
    assert "Permission denied" in client.fail_transfer.call_args.kwargs["error_message"]


def test_permission_error_repairs_task_dir_and_retries(monkeypatch, tmp_path: Path):
    task = tmp_path / "pyscf_hessian" / "6ae0404a4d28e3e70a8c02df"
    task.mkdir(parents=True)
    source = task / "result.wfn.npy"
    source.write_bytes(b"npy")
    monkeypatch.setattr(
        "simstack.core.services.file_transfer_service.context",
        SimpleNamespace(
            config=SimpleNamespace(workdir=tmp_path),
            resource_config=SimpleNamespace(),
        ),
    )
    repaired = []

    def repair(image, task_dir, uid, gid, parent_dir=None):
        repaired.append((image, Path(task_dir), uid, gid, Path(parent_dir)))

    monkeypatch.setattr(
        "simstack.core.run_docker.harmonize_directory_blocking",
        repair,
    )
    monkeypatch.setattr(
        "simstack.core.run_docker.docker_harmonize_image",
        lambda resource_config: "molecular-qm-psi4:latest",
    )
    monkeypatch.setattr(
        "simstack.util.output_permissions.output_owner_ids",
        lambda path: (1000, 1000),
    )
    service = FileTransferService(Resource(value="big2"), interval=10)
    client = Mock()
    client.upload_file.side_effect = [
        PermissionError(13, "Permission denied"),
        {"ok": True},
    ]
    transfer = {
        "transfer_id": "6ae048b124d0cbeb33c57596",
        "source_path": "pyscf_hessian/6ae0404a4d28e3e70a8c02df/result.wfn.npy",
    }

    assert service._upload_transfer(client, transfer) is True
    assert client.upload_file.call_count == 2
    assert repaired == [
        (
            "molecular-qm-psi4:latest",
            task.resolve(),
            1000,
            1000,
            task.parent.resolve(),
        )
    ]
    client.fail_transfer.assert_not_called()
