"""Pinned execution profiles and installation readiness, without external jobs."""

from __future__ import annotations

import importlib
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import zipfile

import pytest

from simstack.runner_runtime.execution_adapter import (
    RunnerExecutionAdapterConfig,
    WorkflowInstallationAdapter,
    WorkerBoundary,
)
from simstack.runner_runtime.workflow_installation import (
    configure_package_imports,
    installation_root,
    packaged_worker_command,
)
from simstack.workflows.archive import archive_digest, extract_archive
from simstack.workflows.dependencies import bootstrap_digests
from simstack.workflows.limits import DEFAULT_INSTALLATION_LIMITS


def _profile():
    return {
        "installer_python": "/operator/python",
        "local_worker_command": ["{python}", "-I", "-m", "simstack.core.run_node"],
        "worker_network_guard_command": ["guard", "--"],
        "worker_environment_allowlist": [],
    }


def _installer_profile():
    return WorkflowInstallationAdapter(
        "/operator/python", WorkerBoundary(("guard", "--"), ())
    )


def _manifest():
    return {
        "workflow": {
            "id": "example.package",
            "version": "1",
            "modules": ["example.nodes"],
            "source_root": ".",
            "runtime_profile": "python-profile",
            "dependencies": [],
        },
        "nodes": {},
        "artifacts": [],
    }


def _cached(tmp_path, installation_id):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, contents in {
            "simstack-workflow.toml": '[workflow]\nid="example.package"\nversion="1"\nmodules=["example.nodes"]\nruntime_profile="python-profile"\ndependencies=[]\n',
            "example/__init__.py": "",
            "example/nodes.py": "def calculate(): pass\n",
        }.items():
            archive.writestr(
                zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), contents
            )
    data = output.getvalue()
    revision = archive_digest(data)
    root = installation_root(tmp_path, installation_id, revision)
    root.mkdir(parents=True)
    (root / "archive.zip").write_bytes(data)
    extract_archive(data, root / "source")
    python = root / "environment" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"test-python")
    python.chmod(0o700)
    (root / "environment" / "pyvenv.cfg").write_text("home = /operator\n")
    site = root / "environment" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "simstack-core-runtime.pth").write_text("/operator/core-site\n")
    (root / "ready.json").write_text(
        json.dumps(
            {
                "revision": revision,
                "manifest": _manifest(),
                "base_python": "/operator/python",
                "core_sites": ["/operator/core-site"],
                "python_sha256": archive_digest(python),
                "bootstrap": bootstrap_digests(root / "environment"),
                "installation_limits": DEFAULT_INSTALLATION_LIMITS.to_dict(),
            }
        )
    )
    descriptor = {
        "installation_id": installation_id,
        "package_id": "example.package",
        "revision": revision,
        "runtime_profile": "python-profile",
        "manifest": _manifest(),
    }
    registry = SimpleNamespace(
        workflow_installation_id=installation_id,
        workflow_revision=revision,
        workflow_package="example.package",
        runtime_profile="python-profile",
    )
    return root, descriptor, registry


def test_shared_profile_executes_new_nodes_without_per_node_hardcoding(tmp_path):
    config = RunnerExecutionAdapterConfig.from_mapping(
        {"resource": {"workflow_profiles": {"python-profile": _profile()}}},
        resource="resource",
        config_directory=tmp_path,
    )
    for node in ("first_new_node", "second_new_node", "arbitrary_package_entry"):
        adapter = config.local(node, runtime_profile="python-profile")
        assert adapter.guarded_worker_command == (
            "guard",
            "--",
            "{python}",
            "-I",
            "-m",
            "simstack.core.run_node",
        )
        assert config.worker_environment(node, runtime_profile="python-profile") == ()


@pytest.mark.parametrize("profile", ["missing-profile", ""])
def test_explicit_runtime_profile_never_falls_back_to_old_program_configuration(
    tmp_path, profile
):
    config = RunnerExecutionAdapterConfig.from_mapping(
        {
            "resource": {
                "program": {
                    "node": {**_profile(), "local_worker_command": ["old-program"]}
                },
                "workflow_profiles": {"python-profile": _profile()},
            }
        },
        resource="resource",
        config_directory=tmp_path,
    )
    with pytest.raises(RuntimeError, match="runtime profile"):
        config.local("node", runtime_profile=profile)
    assert config.local("node").worker_command == ("old-program",)


def test_profile_environment_cannot_reintroduce_database_credentials(tmp_path):
    profile = {**_profile(), "worker_environment_allowlist": ["MONGO_URI"]}
    config = RunnerExecutionAdapterConfig.from_mapping(
        {"resource": {"workflow_profiles": {"python-profile": profile}}},
        resource="resource",
        config_directory=tmp_path,
    )
    with pytest.raises(RuntimeError, match="Database/config variable"):
        config.local("new_node", runtime_profile="python-profile")


def test_worker_command_resolves_exact_installation_even_when_multiple_ids_share_archive_digest(
    tmp_path,
):
    first, first_descriptor, registry = _cached(tmp_path, "tenant-a-installation")
    second, second_descriptor, _ = _cached(tmp_path, "tenant-b-installation")
    assert first_descriptor["revision"] == second_descriptor["revision"]
    command = packaged_worker_command(
        ("guard", "--", "{python}", "{source}", "{runtime}"), registry, workdir=tmp_path
    )
    assert command == (
        "guard",
        "--",
        str(first / "environment" / "bin" / "python"),
        str(first / "source"),
        str(first / "environment"),
    )
    assert str(second) not in " ".join(command)


def test_packaged_task_requires_exact_installation_id_and_never_uses_latest_revision(
    tmp_path,
):
    _, _, registry = _cached(tmp_path, "installation")
    registry.workflow_installation_id = None
    with pytest.raises(ValueError, match="installation identity"):
        packaged_worker_command(("{python}",), registry, workdir=tmp_path)
    registry.workflow_installation_id = "installation"
    registry.workflow_revision = "b" * 64
    with pytest.raises(ValueError, match="exact workflow environment"):
        packaged_worker_command(("{python}",), registry, workdir=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("workflow_package", "other.package"), ("runtime_profile", "other-profile")],
)
def test_task_identity_must_match_readiness_record(tmp_path, field, value):
    _, _, registry = _cached(tmp_path, "installation")
    setattr(registry, field, value)
    with pytest.raises(ValueError, match="identity"):
        packaged_worker_command(("{python}",), registry, workdir=tmp_path)


def test_incomplete_package_binding_does_not_silently_use_legacy_command(tmp_path):
    registry = SimpleNamespace(
        workflow_revision=None,
        workflow_package="example.package",
        runtime_profile="python-profile",
    )
    with pytest.raises(ValueError, match="pinned revision"):
        packaged_worker_command(("legacy-python",), registry, workdir=tmp_path)
    assert packaged_worker_command(
        ("operator-preinstalled-worker",), SimpleNamespace(), workdir=tmp_path
    ) == ("operator-preinstalled-worker",)


def test_unsupported_worker_placeholder_is_not_passed_through_to_process(tmp_path):
    _, _, registry = _cached(tmp_path, "installation")
    with pytest.raises(ValueError, match="unsupported placeholders"):
        packaged_worker_command(
            ("{python}", "{database_uri}"), registry, workdir=tmp_path
        )


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "same-size-source",
        "archive",
        "marker",
        "missing-source",
        "missing-runtime",
    ],
)
def test_worker_import_bootstrap_rejects_stale_or_changed_cache_without_modifying_sys_path(
    monkeypatch, tmp_path, change
):
    root, descriptor, _ = _cached(tmp_path, "installation")
    if change == "source":
        (root / "source" / "example" / "nodes.py").write_text("def different(): pass\n")
    elif change == "same-size-source":
        (root / "source" / "example" / "nodes.py").write_text("def alteredxx(): pass\n")
    elif change == "archive":
        (root / "archive.zip").write_bytes(b"other archive bytes")
    elif change == "marker":
        marker = json.loads((root / "ready.json").read_text())
        marker["manifest"]["workflow"]["id"] = "other.package"
        (root / "ready.json").write_text(json.dumps(marker))
    elif change == "missing-source":
        (root / "source" / "example" / "nodes.py").unlink()
    else:
        (root / "environment" / "bin" / "python").unlink()
    monkeypatch.setattr(sys, "path", ["operator-path"])
    expected_error = "ZIP directory" if change == "archive" else "checksum|manifest|source|runtime"
    with pytest.raises(ValueError, match=expected_error):
        configure_package_imports([descriptor], workdir=tmp_path)
    assert sys.path == ["operator-path"]


def test_worker_import_bootstrap_adds_only_verified_snapshot_without_importing_it(
    monkeypatch, tmp_path
):
    root, descriptor, _ = _cached(tmp_path, "installation")
    monkeypatch.setattr(sys, "path", ["operator-path"])
    configure_package_imports([descriptor], workdir=tmp_path)
    assert sys.path == [str(root / "source"), "operator-path"]
    assert "example.nodes" not in sys.modules


def _service(monkeypatch, tmp_path, *, preparation=None, profile=None):
    module = importlib.import_module(
        "simstack.core.services.workflow_installation_service"
    )
    claim = {
        "package_id": "example.package",
        "workflow_revision": "a" * 64,
        "runtime_profile": "python-profile",
        "manifest": _manifest(),
        "claim_id": "installation-claim",
        "attempt": 3,
    }
    api = SimpleNamespace(
        pending_workflow_installations=AsyncMock(
            return_value=[{"installation_id": "job"}]
        ),
        claim_workflow_installation=AsyncMock(return_value=claim),
        report_workflow_installation=AsyncMock(),
    )
    adapter = SimpleNamespace(
        workflow_installer=MagicMock(return_value=_installer_profile())
    )
    if profile is not None:
        adapter.workflow_installer.side_effect = profile
    fake = SimpleNamespace(
        runner_api=api, runner_adapter=adapter, config=SimpleNamespace(workdir=tmp_path)
    )
    monkeypatch.setattr(module, "context", fake)
    prepare = AsyncMock(
        return_value=tmp_path / "prepared-python", side_effect=preparation
    )
    monkeypatch.setattr(module, "install_package", prepare)
    return module.WorkflowInstallationService("resource", 5), api, adapter, prepare


@pytest.mark.asyncio
async def test_installation_service_reports_ready_only_after_successful_claim_bound_preparation(
    monkeypatch, tmp_path
):
    service, api, adapter, prepare = _service(monkeypatch, tmp_path)
    await service.execute()
    adapter.workflow_installer.assert_called_once_with("python-profile")
    assert api.claim_workflow_installation.await_args.args == ("job",)
    assert api.claim_workflow_installation.await_args.kwargs["request_id"]
    assert prepare.await_args.kwargs == {
        "workdir": tmp_path,
        "profile": _installer_profile(),
        "claim_id": "installation-claim",
        "attempt": 3,
    }
    api.report_workflow_installation.assert_awaited_once_with(
        "job",
        revision="a" * 64,
        claim_id="installation-claim",
        attempt=3,
        status="ready",
        error=None,
    )


@pytest.mark.parametrize(
    "failure", [ValueError("Archive mismatch"), RuntimeError("Dependency unavailable")]
)
@pytest.mark.asyncio
async def test_installation_service_reports_failure_without_claiming_readiness(
    monkeypatch, tmp_path, failure
):
    service, api, _, _ = _service(monkeypatch, tmp_path, preparation=failure)
    await service.execute()
    api.report_workflow_installation.assert_awaited_once_with(
        "job",
        revision="a" * 64,
        claim_id="installation-claim",
        attempt=3,
        status="failed",
        error=str(failure),
    )


@pytest.mark.asyncio
async def test_installation_service_does_not_download_or_fall_back_for_missing_operator_profile(
    monkeypatch, tmp_path
):
    failure = RuntimeError("Workflow runtime profile is not configured")
    service, api, _, prepare = _service(monkeypatch, tmp_path, profile=failure)
    await service.execute()
    prepare.assert_not_awaited()
    assert api.report_workflow_installation.await_args.kwargs["status"] == "failed"
