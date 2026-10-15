"""Package preparation and artifact transport without pip, network or Mongo."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import zipfile

import pytest

from simstack.runner_api import RunnerApiError, SupervisorRunnerClient
from simstack.runner_api.transport import RunnerHttpTransport
from simstack.runner_runtime import workflow_installation as installation
from simstack.runner_runtime.execution_adapter import (
    WorkflowInstallationAdapter,
    WorkerBoundary,
)
from simstack.workflows.archive import archive_digest
from simstack.workflows.manifest import WorkflowManifest
from simstack.workflows.limits import DEFAULT_INSTALLATION_LIMITS
from simstack.workflows.dependencies import bootstrap_digests


def _profile(guard=("guard", "--")):
    return WorkflowInstallationAdapter("/operator/python", WorkerBoundary(guard, ()))


def _manifest(**updates):
    workflow = {
        "id": "example.package",
        "version": "1.0",
        "modules": ["example.nodes"],
        "source_root": ".",
        "runtime_profile": "isolated-python",
        "dependencies": [],
    }
    workflow.update(updates)
    return {"workflow": workflow, "nodes": {}, "artifacts": []}


def _archive(dependencies=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("simstack-workflow.toml", date_time=(1980, 1, 1, 0, 0, 0)),
            '[workflow]\nid="example.package"\nversion="1.0"\n'
            'modules=["example.nodes"]\nruntime_profile="isolated-python"\n'
            f'dependencies={json.dumps(dependencies or [])}\n',
        )
        archive.writestr(
            zipfile.ZipInfo("example/__init__.py", date_time=(1980, 1, 1, 0, 0, 0)), ""
        )
        archive.writestr(
            zipfile.ZipInfo("example/nodes.py", date_time=(1980, 1, 1, 0, 0, 0)),
            "def calculate(): raise AssertionError('body must not run')\n",
        )
    return output.getvalue()


def _cached_package(tmp_path, *, installation_id="installation-1"):
    data = _archive()
    revision = archive_digest(data)
    root = installation.installation_root(tmp_path, installation_id, revision)
    root.mkdir(parents=True)
    (root / "archive.zip").write_bytes(data)
    installation.extract_archive(data, root / "source")
    python = root / "environment" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"operator-owned test executable")
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
                "installation_limits": DEFAULT_INSTALLATION_LIMITS.to_dict(),
                "python_sha256": archive_digest(python),
                "bootstrap": bootstrap_digests(root / "environment"),
            }
        )
    )
    return root, revision, python


def _descriptor(revision, *, installation_id="installation-1", **updates):
    value = {
        "installation_id": installation_id,
        "package_id": "example.package",
        "revision": revision,
        "runtime_profile": "isolated-python",
        "manifest": _manifest(),
    }
    value.update(updates)
    return value


def _claimed_registry(revision, *, installation_id="installation-1"):
    return SimpleNamespace(
        id="507f1f77bcf86cd799439011", workflow_package="example.package",
        workflow_installation_id=installation_id, workflow_revision=revision,
        runtime_profile="isolated-python",
    )


async def _ensure_claimed_runtime(registry, descriptor, workdir, *, profile=None):
    client = SimpleNamespace(task_bootstrap=AsyncMock(return_value={"workflow_packages": [descriptor]}))
    adapter = SimpleNamespace(workflow_installer=MagicMock(return_value=profile or _profile()))
    await installation.ensure_claimed_package_runtime(
        client, registry, claim_id="task-claim", attempt=2, workdir=workdir, adapter=adapter,
    )
    client.task_bootstrap.assert_awaited_once_with(str(registry.id), claim_id="task-claim", attempt=2)


@pytest.mark.asyncio
async def test_claimed_dispatch_checks_valid_cache_without_running_dependency_interpreter(monkeypatch, tmp_path):
    _, revision, _ = _cached_package(tmp_path)
    run = MagicMock(side_effect=AssertionError("Cached runtime must not execute helper Python"))
    monkeypatch.setattr(installation, "_run", run)
    await _ensure_claimed_runtime(_claimed_registry(revision), _descriptor(revision), tmp_path)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_claimed_dispatch_repairs_old_cache_only_from_approved_api_manifest(monkeypatch, tmp_path):
    root, revision, old_python = _cached_package(tmp_path)
    ready = json.loads((root / "ready.json").read_text())
    ready.pop("core_sites")
    (root / "ready.json").write_text(json.dumps(ready))
    commands = _fake_prepare_subprocess(monkeypatch)
    await _ensure_claimed_runtime(_claimed_registry(revision), _descriptor(revision), tmp_path)
    installation._ready_runtime(root, revision=revision, manifest=_manifest())
    assert commands[0][0][0] == "/operator/python" and "venv" in commands[0][0]
    assert all(command[0] != str(old_python) for command, _ in commands)
    assert all("pip" not in command for command, _ in commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["archive", "source", "identity", "unapproved-cache-dependencies", "invalid-api-dependencies"])
async def test_claimed_dispatch_mismatch_never_runs_helpers(monkeypatch, tmp_path, mismatch):
    root, revision, _ = _cached_package(tmp_path)
    descriptor = _descriptor(revision)
    if mismatch == "archive":
        (root / "archive.zip").write_bytes(b"corrupt archive")
    elif mismatch == "source":
        (root / "source" / "example" / "nodes.py").write_text("raise AssertionError('injected')")
    elif mismatch == "identity":
        descriptor["installation_id"] = "another-installation"
    elif mismatch == "unapproved-cache-dependencies":
        ready = json.loads((root / "ready.json").read_text())
        ready["manifest"]["workflow"]["dependencies"] = ["unapproved-package>=1"]
        (root / "ready.json").write_text(json.dumps(ready))
    else:
        descriptor["manifest"]["workflow"]["dependencies"] = ["simstack>=1"]
    run = MagicMock(side_effect=AssertionError("Unapproved package must not execute helpers"))
    monkeypatch.setattr(installation, "_run", run)
    with pytest.raises(ValueError):
        await _ensure_claimed_runtime(_claimed_registry(revision), descriptor, tmp_path)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_claimed_preparation_cancellation_joins_owned_thread_before_return(monkeypatch, tmp_path):
    _, revision, _ = _cached_package(tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def preparation(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        finished.set()

    monkeypatch.setattr(installation, "prepare_package", preparation)
    task = asyncio.create_task(_ensure_claimed_runtime(_claimed_registry(revision), _descriptor(revision), tmp_path))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_claimed_dispatch_prepares_and_repairs_with_real_driver_free_python(monkeypatch, tmp_path):
    if any(importlib.util.find_spec(name) is not None for name in ("pymongo", "motor", "odmantic", "bson")):
        pytest.skip("A driver-free Core interpreter is required for real claimed-package preparation")
    if sys.platform != "linux":
        # Kernel quota enforcement is Linux-only and covered by process-policy
        # tests/Docker acceptance. Keep real bounded helper I/O, venv and probe
        # execution here while substituting only that platform-specific wrapper.
        monkeypatch.setattr("simstack.workflows.processes._limited_command", lambda command, limits: list(command))
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("simstack-workflow.toml", '[workflow]\nid="example.package"\nversion="1.0"\nmodules=["example.nodes"]\nruntime_profile="isolated-python"\ndependencies=[]\n')
        archive.writestr("example/__init__.py", "")
        archive.writestr("example/nodes.py", '''import importlib.util
assert all(importlib.util.find_spec(name) is None for name in ("pymongo", "motor", "odmantic", "bson"))
from simstack.core.node import node
from simstack.models.document import Model
class Result(Model):
    value: int
@node
def calculate(**kwargs) -> Result:
    raise AssertionError("Preparation must never execute the task body")
''')
    revision = archive_digest(data.getvalue())
    root = installation.installation_root(tmp_path, "installation-1", revision)
    root.mkdir(parents=True)
    (root / "archive.zip").write_bytes(data.getvalue())
    # The test interpreter may import Core through PYTHONPATH, which isolated
    # helpers intentionally ignore. Model a properly installed operator base
    # in this test's own venv; do not modify the shared driver-free environment.
    base = tmp_path / "operator"
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(base)], check=True, capture_output=True, timeout=30)
    site = base / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    for entry in Path(sysconfig.get_path("purelib")).iterdir():
        if entry.name != "simstack" and not entry.name.endswith((".pth", ".egg-link")):
            (site / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    (site / "simstack").symlink_to(Path(installation.__file__).parents[1], target_is_directory=True)
    profile = WorkflowInstallationAdapter(
        str(base / "bin" / "python"),
        WorkerBoundary((sys.executable, "-I", "-c", "import os,sys; os.execv(sys.argv[1],sys.argv[1:])"), ()),
    )
    registry = _claimed_registry(revision)
    descriptor = _descriptor(revision)
    await _ensure_claimed_runtime(registry, descriptor, tmp_path, profile=profile)
    ready = json.loads((root / "ready.json").read_text())
    old_environment = (root / "environment").resolve()
    ready.pop("core_sites")
    (root / "ready.json").write_text(json.dumps(ready))
    await _ensure_claimed_runtime(registry, descriptor, tmp_path, profile=profile)
    installation._ready_runtime(root, revision=revision, manifest=descriptor["manifest"])
    assert not old_environment.exists()
    assert (root / "environment" / "bin" / "python").is_file()
    assert "example.nodes" not in sys.modules


def test_cached_source_rejects_added_importable_files(tmp_path):
    root, revision, _ = _cached_package(tmp_path)
    (root / "source" / "example" / "injected.py").write_text("value = 'unregistered'\n")
    with pytest.raises(ValueError, match="unregistered file"):
        installation._ready_runtime(root, revision=revision)


def _transport(monkeypatch, blocks, *, status=200):
    connection = MagicMock()
    connection.getresponse.return_value = SimpleNamespace(
        status=status, read=MagicMock(side_effect=blocks)
    )
    transport = RunnerHttpTransport(
        server_url="https://server.example/proxy/api",
        token=lambda: "resource-scoped-token",
        timeout_seconds=20,
        retry_attempts=0,
        retry_backoff_seconds=0,
    )
    monkeypatch.setattr(transport, "connection", lambda: connection)
    return transport, connection


def test_streamed_archive_is_authenticated_same_origin_and_atomically_published(
    monkeypatch, tmp_path
):
    data = _archive()
    transport, connection = _transport(monkeypatch, [data[:11], data[11:], b""])
    destination = tmp_path / "packages" / "archive.zip"
    transport.download(
        "/workflow-installations/job/archive?claim_id=claim&attempt=2",
        destination,
        sha256=archive_digest(data),
    )
    assert destination.read_bytes() == data
    assert list(destination.parent.glob(".archive-*")) == []
    args, kwargs = connection.request.call_args
    assert args == (
        "GET",
        "/proxy/api/runner/workflow-installations/job/archive?claim_id=claim&attempt=2",
    )
    assert kwargs["headers"] == {
        "Authorization": "Bearer resource-scoped-token",
        "Accept": "application/zip",
    }
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    "failure", ["checksum", "interrupted", "unauthorized", "redirect"]
)
def test_failed_archive_download_preserves_existing_cache_and_removes_partial_bytes(
    monkeypatch, tmp_path, failure
):
    data = _archive()
    status = {"unauthorized": 403, "redirect": 302}.get(failure, 200)
    blocks = (
        [b"untrusted bytes", b""]
        if failure == "checksum"
        else [data[:7], OSError("connection lost")]
    )
    transport, connection = _transport(monkeypatch, blocks, status=status)
    destination = tmp_path / "archive.zip"
    destination.write_bytes(b"previous verified bytes")
    with pytest.raises(RunnerApiError):
        transport.download(
            "/workflow-installations/job/archive",
            destination,
            sha256=archive_digest(data),
        )
    assert destination.read_bytes() == b"previous verified bytes"
    assert list(tmp_path.glob(".archive-*")) == []
    connection.close.assert_called_once()


def test_streamed_archive_over_budget_preserves_cache_and_removes_partial_download(
    monkeypatch, tmp_path
):
    data = _archive()
    transport, connection = _transport(monkeypatch, [data[:7], data[7:14], data[14:], b""])
    destination = tmp_path / "archive.zip"
    destination.write_bytes(b"previous verified archive")
    with pytest.raises(RunnerApiError, match="compressed size"):
        transport.download(
            "/workflow-installations/job/archive", destination,
            sha256=archive_digest(data), max_bytes=10,
        )
    assert destination.read_bytes() == b"previous verified archive"
    assert list(tmp_path.glob(".archive-*")) == []
    assert connection.getresponse.return_value.read.call_count == 2
    connection.close.assert_called_once()


@pytest.mark.parametrize("max_bytes", [False, 0, -1, 1.5])
def test_archive_transport_budget_is_validated_before_http(monkeypatch, tmp_path, max_bytes):
    transport, connection = _transport(monkeypatch, [])
    with pytest.raises(RunnerApiError, match="budget must be positive"):
        transport.download(
            "/workflow-installations/job/archive", tmp_path / "archive.zip",
            sha256="a" * 64, max_bytes=max_bytes,
        )
    connection.request.assert_not_called()


@pytest.mark.parametrize("revision", ["", "a" * 63, "A" * 64, "../" + "a" * 61])
def test_artifact_digest_must_be_canonical_before_http(monkeypatch, tmp_path, revision):
    transport, connection = _transport(monkeypatch, [])
    with pytest.raises(RunnerApiError, match="canonical SHA256"):
        transport.download(
            "/workflow-installations/job/archive",
            tmp_path / "archive.zip",
            sha256=revision,
        )
    connection.request.assert_not_called()


def test_installation_directory_hashes_opaque_id_and_is_pinned_to_exact_revision(
    tmp_path,
):
    opaque = "../../different-user/installation"
    revision = "a" * 64
    root = installation.installation_root(tmp_path, opaque, revision)
    assert (
        root
        == tmp_path
        / "workflow-packages"
        / hashlib.sha256(opaque.encode()).hexdigest()
        / revision
    )
    root.relative_to(tmp_path)


def test_preparation_rejects_corrupt_archive_before_any_subprocess(
    monkeypatch, tmp_path
):
    root = tmp_path / "package"
    root.mkdir()
    (root / "archive.zip").write_bytes(b"corrupt")
    run = MagicMock()
    monkeypatch.setattr(installation, "_run", run)
    with pytest.raises(ValueError, match="ZIP directory"):
        installation.prepare_package(
            root,
            revision="a" * 64,
            manifest=_manifest(),
            base_python="/operator/python",
            guard=("guard",),
        )
    run.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "other.package"),
        ("version", "2"),
        ("modules", ["other.nodes"]),
        ("runtime_profile", "other-profile"),
    ],
)
def test_preparation_rejects_archive_manifest_identity_mismatch(
    monkeypatch, tmp_path, field, value
):
    root, revision, _ = _cached_package(tmp_path)
    monkeypatch.setattr(
        installation, "_run", MagicMock(side_effect=AssertionError("must not execute"))
    )
    with pytest.raises(ValueError, match=field):
        installation.prepare_package(
            root,
            revision=revision,
            manifest=_manifest(**{field: value}),
            base_python="/operator/python",
            guard=("guard",),
        )


def test_cached_preparation_is_idempotent_without_pip_or_importing_code(
    monkeypatch, tmp_path
):
    root, revision, python = _cached_package(tmp_path)
    run = MagicMock(
        side_effect=AssertionError("cached preparation must not execute code")
    )
    monkeypatch.setattr(installation, "_run", run)
    assert (
        installation.prepare_package(
            root,
            revision=revision,
            manifest=_manifest(),
            base_python="/operator/python",
            guard=("guard",),
        )
        == python
    )
    run.assert_not_called()


@pytest.mark.parametrize("tamper", ["namespace", "transitive-distribution", "startup-hook", "operator-path", "marker", "executable", "bootstrap"])
def test_cached_runner_environment_is_validated_without_executing_dependency_python(
    monkeypatch, tmp_path, tamper
):
    root, revision, _ = _cached_package(tmp_path)
    site = root / "environment" / "lib" / "python3.12" / "site-packages"
    if tamper == "namespace":
        (site / "simstack.py").write_text("raise AssertionError('must not execute')")
    elif tamper == "transitive-distribution":
        metadata = site / "renamed_dependency-1.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: SimStack\nVersion: 1\n")
    elif tamper == "startup-hook":
        (site / "startup.pth").write_text("import untrusted\n")
    elif tamper == "operator-path":
        (site / "simstack-core-runtime.pth").write_text("/attacker/core\n")
    elif tamper == "marker":
        ready = json.loads((root / "ready.json").read_text())
        ready.pop("core_sites")
        (root / "ready.json").write_text(json.dumps(ready))
    elif tamper == "executable":
        (root / "environment" / "bin" / "python").write_bytes(b"untrusted interpreter replacement")
    else:
        (root / "environment" / "pyvenv.cfg").write_text("home = /attacker\n")
    run = MagicMock(side_effect=AssertionError("must not start dependency interpreter"))
    monkeypatch.setattr(installation, "_run", run)
    with pytest.raises(ValueError, match="protected|startup|Core paths|executable|interpreter|bootstrap"):
        installation._ready_runtime(root, revision=revision, manifest=_manifest())
    run.assert_not_called()


def test_explicit_installer_retry_rebuilds_rejected_cache_using_only_trusted_python(
    monkeypatch, tmp_path
):
    root, revision, rejected_python = _cached_package(tmp_path)
    site = root / "environment" / "lib" / "python3.12" / "site-packages"
    (site / "startup.pth").write_text("import untrusted\n")
    commands = _fake_prepare_subprocess(monkeypatch)
    rebuilt = installation.prepare_package(
        root, revision=revision, manifest=_manifest(),
        base_python="/operator/python", guard=("guard",),
    )
    assert rebuilt.is_file()
    assert "venv" in commands[0][0]
    assert commands[0][0][0] == "/operator/python"
    assert all(command[0] != str(rejected_python) for command, _ in commands)
    assert not (root / "environment" / "lib" / "python3.12" / "site-packages" / "startup.pth").exists()
    installation._ready_runtime(root, revision=revision, manifest=_manifest())


def test_cached_preparation_still_requires_operator_owned_guard(monkeypatch, tmp_path):
    root, revision, _ = _cached_package(tmp_path)
    monkeypatch.setattr(
        installation, "_run", MagicMock(side_effect=AssertionError("must not execute"))
    )
    with pytest.raises(ValueError, match="boundary|guard"):
        installation.prepare_package(
            root,
            revision=revision,
            manifest=_manifest(),
            base_python="/operator/python",
            guard=(),
        )


def test_cached_preparation_rejects_missing_runtime_executable(monkeypatch, tmp_path):
    root, revision, python = _cached_package(tmp_path)
    python.unlink()
    monkeypatch.setattr(
        installation, "_run", MagicMock(side_effect=AssertionError("must not execute"))
    )
    with pytest.raises(ValueError, match="runtime|environment|executable"):
        installation.prepare_package(
            root,
            revision=revision,
            manifest=_manifest(),
            base_python="/operator/python",
            guard=("guard",),
        )


def _fake_prepare_subprocess(monkeypatch, *, failure=None):
    commands = []

    def run(command, *, input_data=None, **_):
        commands.append((command, input_data))
        stdout = ""
        if "venv" in command:
            staging = Path(command[-1])
            python = staging / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"test-python")
            python.chmod(0o700)
            (staging / "pyvenv.cfg").write_text("home = /operator\n")
            (staging / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
        elif "-c" in command:
            stdout = json.dumps(
                [3, 12]
                if "sys.version_info" in command[-1]
                else ["/operator/core-site", "/operator/core-site"]
            )
        elif "simstack.workflows.runtime" in command:
            if failure:
                raise subprocess.CalledProcessError(1, command, stderr=failure)
            stdout = json.dumps({"ok": True, "catalog": {}})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(installation, "_run", run)
    return commands


def test_preparation_publishes_verified_runtime_atomically_and_uses_guard_for_imports(
    monkeypatch, tmp_path
):
    data = _archive()
    root = tmp_path / "package"
    root.mkdir()
    (root / "archive.zip").write_bytes(data)
    commands = _fake_prepare_subprocess(monkeypatch)
    python = installation.prepare_package(
        root,
        revision=archive_digest(data),
        manifest=_manifest(),
        base_python="/operator/python",
        guard=("resource-guard", "--"),
    )
    assert python.is_file()
    assert (root / "environment").is_symlink()
    assert json.loads((root / "ready.json").read_text())["revision"] == archive_digest(
        data
    )
    assert list(root.glob(".ready-*")) == []
    probe = next(
        command for command, _ in commands if "simstack.workflows.runtime" in command
    )
    assert probe[:2] == ["resource-guard", "--"]
    assert "-I" in probe and "-B" in probe
    assert all("pip" not in command for command, _ in commands)
    assert (
        python.parent.parent
        / "lib"
        / "python3.12"
        / "site-packages"
        / "simstack-core-runtime.pth"
    ).read_text() == "/operator/core-site\n"


@pytest.mark.parametrize("module", ["simstack", "sitecustomize.py", "executable", "bootstrap"])
def test_newly_installed_dependency_namespace_is_rejected_before_runtime_probe(
    monkeypatch, tmp_path, module
):
    data = _archive(["example-dependency>=1"])
    root = tmp_path / "package"
    root.mkdir()
    (root / "archive.zip").write_bytes(data)
    commands = _fake_prepare_subprocess(monkeypatch)
    fake_run = installation._run
    def install_untrusted_dependency(command, **kwargs):
        result = fake_run(command, **kwargs)
        if "pip" in command:
            environment = Path(command[0]).parent.parent
            site = environment / "lib" / "python3.12" / "site-packages"
            if module == "executable":
                Path(command[0]).write_bytes(b"untrusted interpreter replacement")
            elif module == "bootstrap":
                (environment / "pyvenv.cfg").write_text("home = /attacker\n")
            else:
                (site / module).write_text("raise AssertionError('never execute dependency code')")
        return result
    monkeypatch.setattr(installation, "_run", install_untrusted_dependency)
    with pytest.raises(ValueError, match="protected module|executable|interpreter|bootstrap"):
        installation.prepare_package(
            root, revision=archive_digest(data),
            manifest=_manifest(dependencies=["example-dependency>=1"]),
            base_python="/operator/python", guard=("guard",),
        )
    assert any("pip" in command for command, _ in commands)
    assert not any("simstack.workflows.runtime" in command for command, _ in commands)
    assert not (root / "ready.json").exists()
    assert list(root.glob(".environment-*")) == []


def test_failed_preparation_cleans_only_its_own_staging_and_remains_retryable(
    monkeypatch, tmp_path
):
    data = _archive()
    root = tmp_path / "package"
    root.mkdir()
    (root / "archive.zip").write_bytes(data)
    preserved = root / ".environment-existing"
    preserved.mkdir()
    (preserved / "operator-file").write_text("keep")
    _fake_prepare_subprocess(monkeypatch, failure="import error")
    with pytest.raises(subprocess.CalledProcessError):
        installation.prepare_package(
            root,
            revision=archive_digest(data),
            manifest=_manifest(),
            base_python="/operator/python",
            guard=("guard",),
        )
    assert list(root.glob(".environment-*")) == [preserved]
    assert (preserved / "operator-file").read_text() == "keep"
    assert not (root / "ready.json").exists()
    assert not (root / "environment").exists()
    _fake_prepare_subprocess(monkeypatch)
    assert installation.prepare_package(
        root,
        revision=archive_digest(data),
        manifest=_manifest(),
        base_python="/operator/python",
        guard=("guard",),
    ).is_file()


@pytest.mark.parametrize("source_name", ["source", ".source-pending"])
def test_hard_crash_during_extraction_is_recovered_without_running_partial_source(monkeypatch, tmp_path, source_name):
    data = _archive()
    root = tmp_path / "package"
    root.mkdir()
    archive = root / "archive.zip"
    archive.write_bytes(data)
    source = root / source_name
    process = subprocess.run([
        sys.executable, "-B", "-c", """
import os, sys
from pathlib import Path
from simstack.workflows.archive import extract_archive
original = Path.open
def interrupted(path, *args, **kwargs):
    if path.name == '__init__.py' and args and args[0] == 'xb':
        os._exit(77)
    return original(path, *args, **kwargs)
Path.open = interrupted
extract_archive(Path(sys.argv[1]), Path(sys.argv[2]))
""", str(archive), str(source)], timeout=15, capture_output=True)
    assert process.returncode == 77, process.stderr
    assert (source / "simstack-workflow.toml").is_file()
    assert not (source / "example" / "nodes.py").exists()
    _fake_prepare_subprocess(monkeypatch)
    python = installation.prepare_package(root, revision=archive_digest(data), manifest=_manifest(), base_python="/operator/python", guard=("guard",))
    assert python.is_file()
    assert (root / "source" / "example" / "nodes.py").is_file()
    assert not (root / ".source-pending").exists()
    installation._verify_source_snapshot(archive, root / "source")


def test_ready_snapshot_rejects_changed_executable_permission(monkeypatch, tmp_path):
    root, revision, _ = _cached_package(tmp_path)
    (root / "source" / "example" / "nodes.py").chmod(0o700)
    monkeypatch.setattr(installation, "_run", MagicMock(side_effect=AssertionError("never execute changed snapshot")))
    with pytest.raises(ValueError, match="executable permissions"):
        installation.prepare_package(root, revision=revision, manifest=_manifest(), base_python="/operator/python", guard=("guard",))
    assert (root / "ready.json").exists()


def test_preparation_helper_removes_database_and_api_secrets_from_subprocess_environment(
    monkeypatch,
):
    monkeypatch.setenv("MONGO_URI", "must-not-leak")
    monkeypatch.setenv("DB_PASSWORD", "must-not-leak")
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "must-not-leak")
    monkeypatch.setenv("LANG", "ru_RU.KOI8-R")
    monkeypatch.setenv("LC_ALL", "fr_FR.ISO8859-1")
    monkeypatch.setenv("LC_CTYPE", "de_DE.ISO8859-1")
    for name in DEFAULT_INSTALLATION_LIMITS.native_thread_environment():
        monkeypatch.setenv(name, "999")
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
    )
    monkeypatch.setattr(installation, "run_process", run)
    limits = replace(DEFAULT_INSTALLATION_LIMITS, max_process_tree_cpu_cores=6, max_parallel_processes=2)
    installation._run(["operator-python", "-I", "-m", "venv", "/owned/staging"], limits=limits)
    environment = run.call_args.kwargs["env"]
    assert {"MONGO_URI", "DB_PASSWORD", "SIMSTACK_RUNNER_TOKEN"}.isdisjoint(environment)
    assert environment["LANG"] == "C.UTF-8"
    assert environment["LC_ALL"] == "C.UTF-8"
    assert all(environment[name] == "3" for name in limits.native_thread_environment())
    assert json.loads(environment["SIMSTACK_WORKFLOW_NATIVE_THREAD_POLICY"]) == limits.native_thread_environment()
    assert json.loads(environment["SIMSTACK_WORKFLOW_INSTALL_LIMITS"]) == limits.to_dict()


def test_preparation_helper_preserves_text_result_and_failure(monkeypatch):
    command = ["trusted-tool"]
    run = MagicMock(return_value=subprocess.CompletedProcess(command, 0, b"reply", b"diagnostic"))
    monkeypatch.setattr(installation, "run_process", run)
    result = installation._run(command, input_data={"operation": "inspect"})
    assert result.stdout == "reply"
    assert result.stderr == "diagnostic"
    assert run.call_args.kwargs["input_data"] == b'{"operation": "inspect"}'
    run.return_value = subprocess.CompletedProcess(command, 7, b"reply", b"diagnostic")
    with pytest.raises(subprocess.CalledProcessError) as error:
        installation._run(command)
    assert error.value.returncode == 7
    assert error.value.output == "reply"
    assert error.value.stderr == "diagnostic"


def test_concurrent_preparations_share_one_verified_runtime(monkeypatch, tmp_path):
    data = _archive()
    root = tmp_path / "package"
    root.mkdir()
    (root / "archive.zip").write_bytes(data)
    commands = _fake_prepare_subprocess(monkeypatch)

    def prepare():
        return installation.prepare_package(
            root,
            revision=archive_digest(data),
            manifest=_manifest(),
            base_python="/operator/python",
            guard=("guard",),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(prepare), executor.submit(prepare)]
        results = [future.result(timeout=5) for future in futures]
    assert results[0] == results[1]
    assert sum("venv" in command for command, _ in commands) == 1
    assert len(list(root.glob(".environment-*"))) == 1


@pytest.mark.asyncio
async def test_installer_only_downloads_via_claim_bound_server_api(
    monkeypatch, tmp_path
):
    descriptor = _descriptor(archive_digest(_archive()))
    client = SimpleNamespace(download_workflow_archive=AsyncMock())
    expected = tmp_path / "prepared-python"
    prepare = MagicMock(return_value=expected)
    monkeypatch.setattr(installation, "prepare_package", prepare)
    assert (
        await installation.install_package(
            client,
            descriptor,
            workdir=tmp_path,
            profile=_profile(),
            claim_id="claim-2",
            attempt=3,
        )
        == expected
    )
    root = installation.installation_root(
        tmp_path, descriptor["installation_id"], descriptor["revision"]
    )
    client.download_workflow_archive.assert_awaited_once_with(
        "installation-1",
        root / "archive.zip",
        revision=descriptor["revision"],
        claim_id="claim-2",
        attempt=3,
        max_bytes=DEFAULT_INSTALLATION_LIMITS.max_archive_bytes,
    )
    assert prepare.call_args.kwargs["guard"] == ("guard", "--")


@pytest.mark.parametrize(
    "profile",
    [
        {},
        _profile(()),
        _profile((1,)),
        _profile(("",)),
    ],
)
@pytest.mark.asyncio
async def test_installer_fails_closed_for_missing_or_invalid_runtime_boundary(
    monkeypatch, tmp_path, profile
):
    client = SimpleNamespace(download_workflow_archive=AsyncMock())
    prepare = MagicMock()
    monkeypatch.setattr(installation, "prepare_package", prepare)
    with pytest.raises(ValueError, match="profile|boundary|guard"):
        await installation.install_package(
            client,
            _descriptor(archive_digest(_archive())),
            workdir=tmp_path,
            profile=profile,
            claim_id="claim",
            attempt=1,
        )
    prepare.assert_not_called()


@pytest.mark.parametrize("field", ["package_id", "runtime_profile"])
@pytest.mark.asyncio
async def test_installer_rejects_descriptor_that_disagrees_with_its_manifest(
    monkeypatch, tmp_path, field
):
    client = SimpleNamespace(download_workflow_archive=AsyncMock())
    prepare = MagicMock()
    monkeypatch.setattr(installation, "prepare_package", prepare)
    with pytest.raises(ValueError, match="identity|manifest|profile|package"):
        await installation.install_package(
            client,
            _descriptor(archive_digest(_archive()), **{field: "different.identity"}),
            workdir=tmp_path,
            profile=_profile(),
            claim_id="claim",
            attempt=1,
        )
    prepare.assert_not_called()


@pytest.mark.parametrize(
    "dependency", ["pymongo>=4", "Motor", "ODMantic", "bson", "simstack==1"]
)
def test_manifest_cannot_install_database_runtime_or_replace_core(dependency):
    with pytest.raises(ValueError, match="database drivers|replace SimStack"):
        WorkflowManifest.model_validate(_manifest(dependencies=[dependency]))


@pytest.mark.asyncio
async def test_archive_api_uses_claim_and_attempt_not_user_database_parameters(
    monkeypatch, tmp_path
):
    client = SupervisorRunnerClient(
        server_url="https://server.example", runner_token="resource-token"
    )
    download = MagicMock()
    monkeypatch.setattr(client._transport, "download", download)
    revision = "a" * 64
    destination = tmp_path / "archive.zip"
    await client.download_workflow_archive(
        "job", destination, revision=revision, claim_id="lease+scope", attempt=2
    )
    download.assert_called_once_with(
        "/workflow-installations/job/archive?claim_id=lease%2Bscope&attempt=2",
        destination,
        sha256=revision,
    )


@pytest.mark.asyncio
async def test_installation_status_reports_include_claim_revision_and_attempt(
    monkeypatch,
):
    client = SupervisorRunnerClient(
        server_url="https://server.example", runner_token="resource-token"
    )
    request = AsyncMock(return_value={"status": "ready"})
    monkeypatch.setattr(client, "_request", request)
    await client.report_workflow_installation(
        "job",
        revision="a" * 64,
        claim_id="claim-3",
        attempt=4,
        status="failed",
        error="dependency preparation failed",
    )
    request.assert_awaited_once_with(
        "POST",
        "/workflow-installations/job/status",
        {
            "workflow_revision": "a" * 64,
            "claim_id": "claim-3",
            "attempt": 4,
            "status": "failed",
            "error": "dependency preparation failed",
        },
        retry_safe=True,
    )
    with pytest.raises(RunnerApiError, match="Input should be 'ready' or 'failed'"):
        await client.report_workflow_installation(
            "job", revision="a" * 64, claim_id="claim-3", attempt=4, status="active"
        )
    assert request.await_count == 1
