"""uv graph preservation, platform compatibility, and safe preparation phases."""

from __future__ import annotations

import json
import sys

import pytest

from simstack.workflows.manifest import load_manifest
from simstack.workflows.uv_project import (
    load_uv_project,
    uv_preparation_commands,
    validate_platform_core,
)


def _project(tmp_path, *, dependencies="[]", extra_lock="", core=""):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="test-flow"\nversion="1.0"\n'
        f"dependencies={dependencies}\n"
        '[build-system]\nrequires=["hatchling>=1"]\nbuild-backend="hatchling.build"\n'
    )
    (tmp_path / "uv.lock").write_text(
        'version=1\nrevision=3\nrequires-python=">=3.12"\n'
        '[[package]]\nname="test-flow"\nversion="1.0"\nsource={editable="."}\n'
        + core
        + extra_lock
    )
    (tmp_path / "simstack-workflow.toml").write_text(
        '[workflow]\nid="test.flow"\nversion="1"\nmodules=["example"]\nruntime_profile="python"\n'
    )
    return tmp_path


def test_manifest_only_archive_retains_legacy_contract(tmp_path):
    assert load_uv_project(tmp_path) is None


def test_locked_project_requires_pyproject(tmp_path):
    (tmp_path / "uv.lock").write_text("version=1\npackage=[]\n")
    with pytest.raises(ValueError, match="regular file"):
        load_uv_project(tmp_path)


def test_manifest_dependencies_do_not_replace_project_lock(tmp_path):
    root = _project(tmp_path, dependencies="[\"numpy>=2; sys_platform == 'linux'\"]")
    manifest = load_manifest(root)
    assert manifest.workflow.dependencies == []
    project = load_uv_project(root)
    assert project.dependencies == ("numpy>=2; sys_platform == 'linux'",)
    assert project.source_packages == ("test-flow",)
    before = (root / "uv.lock").read_bytes()
    assert load_uv_project(root).identity == project.identity
    assert (root / "uv.lock").read_bytes() == before
    with (root / "simstack-workflow.toml").open("a") as stream:
        stream.write('dependencies=["numpy"]\n')
    with pytest.raises(ValueError, match="second manifest list"):
        load_manifest(root)


@pytest.mark.parametrize("dependency", ["odmantic>=1", "motor", "PyMongo", "bson"])
def test_direct_database_dependencies_require_explicit_migration(tmp_path, dependency):
    root = _project(tmp_path, dependencies=json.dumps([dependency]))
    with pytest.raises(ValueError, match="migrate database dependencies"):
        load_uv_project(root)


def test_inactive_dependency_marker_is_not_selected_by_custom_graph_resolution(
    tmp_path,
):
    root = _project(tmp_path, dependencies="[\"odmantic; python_version < '2'\"]")
    assert load_uv_project(root).dependencies == ("odmantic; python_version < '2'",)


def test_unbundled_git_dependency_is_rejected_before_installation(tmp_path):
    root = _project(
        tmp_path,
        extra_lock='[[package]]\nname="science"\nversion="1"\nsource={git="https://example.invalid/science.git#'
        + "a" * 40
        + '"}\n',
    )
    with pytest.raises(ValueError, match="not bundled"):
        load_uv_project(root)


def test_contained_source_dependency_keeps_locked_identity(tmp_path):
    root = _project(
        tmp_path,
        extra_lock='[[package]]\nname="science"\nversion="2"\nsource={directory="vendor/science"}\n',
    )
    source = root / "vendor" / "science"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname="science"\nversion="2"\n[build-system]\nrequires=["setuptools>=61"]\nbuild-backend="setuptools.build_meta"\n'
    )
    project = load_uv_project(root)
    assert project.source_packages == ("science", "test-flow")
    assert project.build_requirements == ("setuptools>=61", "hatchling>=1")


def test_source_cannot_escape_snapshot(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    _project(
        root,
        extra_lock='[[package]]\nname="science"\nversion="1"\nsource={directory="../outside"}\n',
    )
    with pytest.raises(ValueError, match="escapes"):
        load_uv_project(root)


@pytest.mark.parametrize(
    "url",
    [
        "http://pypi.org/simple",
        "https://user:secret@pypi.org/simple",
        "https://127.0.0.1/simple",
        "https://pypi.org:8443/simple",
        "https://metadata.google.internal/simple",
    ],
)
def test_registry_cannot_request_server_private_networks(tmp_path, url):
    root = _project(
        tmp_path,
        extra_lock='[[package]]\nname="science"\nversion="1"\nsource={registry='
        + json.dumps(url)
        + "}\n",
    )
    with pytest.raises(ValueError, match="approved public PyPI"):
        load_uv_project(root)


def test_wheel_url_cannot_override_approved_registry(tmp_path):
    root = _project(
        tmp_path,
        extra_lock='[[package]]\nname="science"\nversion="1"\nsource={registry="https://pypi.org/simple"}\nwheels=[{url="https://127.0.0.1/private.whl",hash="sha256:abcd"}]\n',
    )
    with pytest.raises(ValueError, match="approved public PyPI"):
        load_uv_project(root)


def test_core_revision_is_not_silently_replaced(tmp_path):
    sha = "a" * 40
    root = _project(
        tmp_path,
        core='[[package]]\nname="simstack"\nversion="2.1"\nsource={git="https://github.com/simstack/simstack.git#'
        + sha
        + '"}\n',
    )
    project = load_uv_project(root)
    validate_platform_core(
        project, {"version": "2.1", "direct_url": {"vcs_info": {"commit_id": sha}}}
    )
    validate_platform_core(project, {"version": "2.1", "commit_id": sha})
    with pytest.raises(ValueError, match="different SimStack Core version"):
        validate_platform_core(project, {"version": "2.0", "commit_id": sha})
    with pytest.raises(ValueError, match="different SimStack Core revision"):
        validate_platform_core(project, {"version": "2.1", "commit_id": "b" * 40})
    with pytest.raises(ValueError, match="different SimStack Core revision"):
        validate_platform_core(project, {"version": "2.1"})


def test_build_backend_cannot_fetch_an_arbitrary_url(tmp_path):
    root = _project(tmp_path)
    path = root / "pyproject.toml"
    path.write_text(
        path.read_text().replace(
            "hatchling>=1", "backend @ https://127.0.0.1/private.whl"
        )
    )
    with pytest.raises(ValueError, match="ordinary package-index"):
        load_uv_project(root)


def test_uv_phases_use_frozen_graph_and_source_build_is_offline(tmp_path):
    root = _project(tmp_path)
    uv = tmp_path / "uv"
    uv.write_text("operator binary placeholder")
    environment = root / "environment"
    commands = uv_preparation_commands(
        load_uv_project(root), environment, sys.executable, uv=str(uv)
    )
    prefetch = commands["prefetch"]
    assert (
        "--frozen" in prefetch
        and "--no-build" in prefetch
        and "--no-install-project" in prefetch
    )
    assert (
        "--no-install-package" in prefetch
        and "test-flow" in prefetch
        and "simstack" in prefetch
    )
    assert "--inexact" in prefetch
    assert "--only-binary" in commands["build_dependencies"][3]
    assert "--target" in commands["build_dependencies"][3]
    assert commands["build_dependencies"][6] == sys.executable
    sync = commands["sync"]
    assert (
        "--offline" in sync
        and "--no-editable" in sync
        and "--no-build-isolation" not in sync
    )
    assert "--inexact" in sync and "--frozen" in sync
    settings = json.loads(sync[4])
    assert settings["UV_PROJECT_ENVIRONMENT"] == str(environment)
    assert settings["UV_KEYRING_PROVIDER"] == "disabled"
    assert settings["UV_PYTHON_DOWNLOADS"] == "never"


def _fake_locked_package(tmp_path, monkeypatch, *, corrupt_prefetch=False):
    import io
    import subprocess
    import zipfile
    from pathlib import Path

    from simstack.runner_runtime import workflow_installation as installation
    from simstack.workflows.archive import archive_digest

    project = tmp_path / "input"
    project.mkdir()
    _project(project)
    (project / "example.py").write_text("ANSWER = 42\n")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for path in project.iterdir():
            archive.writestr(path.name, path.read_bytes())
    data = content.getvalue()
    revision = archive_digest(data)
    root = tmp_path / "prepared"
    root.mkdir()
    (root / "archive.zip").write_bytes(data)
    uv = tmp_path / "operator-uv"
    uv.write_text("operator uv placeholder")
    monkeypatch.setenv("SIMSTACK_UV_BINARY", str(uv))
    calls = []
    state = {}

    def run(command, **options):
        calls.append((command, options))
        output = "{}"
        if "venv" in command:
            environment = Path(command[-1])
            (environment / "bin").mkdir()
            python = environment / "bin" / "python"
            python.write_bytes(b"operator-owned interpreter")
            python.chmod(0o700)
            (environment / "pyvenv.cfg").write_text("home = /operator\n")
            site = environment / "lib" / "python3.12" / "site-packages"
            site.mkdir(parents=True)
            state.update(environment=environment, site=site)
        elif "-c" in command and "importlib.metadata.distribution" in command[-1]:
            output = json.dumps({"version": "2.1"})
        elif "-c" in command and "sysconfig" in command[-1]:
            output = json.dumps(["/operator/core-site"])
        elif "-c" in command and "version_info" in command[-1]:
            output = "[3, 12]"
        elif "--no-build" in command:
            if corrupt_prefetch:
                (state["site"] / "sitecustomize.py").write_text(
                    "raise RuntimeError('must never run')"
                )
        elif "sync" in command:
            assert command[:2] == ["operator-guard", "--"]
            (state["site"] / "example_installed.py").write_text("ANSWER = 42\n")
        elif options.get("input_data", {}).get("operation") == "discover":
            output = json.dumps({"ok": True})
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(installation, "_run", run)
    return installation, root, revision, load_manifest(project).model_dump(), calls


def test_runner_builds_sources_only_behind_boundary_and_records_lock_identity(
    tmp_path, monkeypatch
):
    installation, root, revision, manifest, calls = _fake_locked_package(
        tmp_path, monkeypatch
    )
    installation.prepare_package(
        root,
        revision=revision,
        manifest=manifest,
        base_python="/operator/python",
        guard=("operator-guard", "--"),
    )
    ready = installation._ready_runtime(root, revision=revision, manifest=manifest)
    assert ready["project_identity"] == load_uv_project(root / "source").identity
    full_sync = [
        command for command, _ in calls if "sync" in command and "--offline" in command
    ]
    assert len(full_sync) == 1 and full_sync[0][:2] == ["operator-guard", "--"]
    assert (
        root
        / "environment"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "example_installed.py"
    ).is_file()
    assert not (root / ".uv-build-source").exists()


def test_runner_rejects_dependency_startup_hooks_before_build_or_import(
    tmp_path, monkeypatch
):
    installation, root, revision, manifest, calls = _fake_locked_package(
        tmp_path, monkeypatch, corrupt_prefetch=True
    )
    with pytest.raises(ValueError, match="protected module"):
        installation.prepare_package(
            root,
            revision=revision,
            manifest=manifest,
            base_python="/operator/python",
            guard=("operator-guard", "--"),
        )
    assert not any("--offline" in command for command, _ in calls)
    assert not any(
        options.get("input_data", {}).get("operation") == "discover"
        for _, options in calls
    )
    assert not (root / "ready.json").exists()
    assert not (root / "environment").exists()
    assert not (root / ".uv-build-source").exists()


def test_conflicting_backend_versions_are_prefetched_as_separate_build_groups(tmp_path):
    extra = ""
    for label in ("a", "b"):
        extra += f'[[package]]\nname="backend-{label}"\nversion="1"\nsource={{directory="vendor/{label}"}}\n'
    root = _project(tmp_path, extra_lock=extra)
    for label, version in (("a", "75.0.0"), ("b", "80.9.0")):
        source = root / "vendor" / label
        source.mkdir(parents=True)
        (source / "pyproject.toml").write_text(
            f'[project]\nname="backend-{label}"\nversion="1"\n'
            f'[build-system]\nrequires=["setuptools=={version}","wheel"]\nbuild-backend="setuptools.build_meta"\n'
        )
    project = load_uv_project(root)
    assert ("setuptools==75.0.0", "wheel") in project.build_requirement_groups
    assert ("setuptools==80.9.0", "wheel") in project.build_requirement_groups
    uv = root / "uv"
    uv.write_text("operator binary placeholder")
    commands = uv_preparation_commands(
        project, root / "environment", sys.executable, uv=str(uv)
    )
    groups = json.loads(commands["build_dependencies"][-1])
    assert ["setuptools==75.0.0", "wheel"] in groups
    assert ["setuptools==80.9.0", "wheel"] in groups
    assert not any(
        "setuptools==75.0.0" in group and "setuptools==80.9.0" in group
        for group in groups
    )


@pytest.mark.parametrize(
    "settings",
    [
        '[tool.uv.sources]\nscience={git="https://github.com/example/science.git"}\n',
        '[tool.uv.workspace]\nmembers=["packages/*"]\n',
        "[tool.uv]\npackage=true\n",
        "[tool.uv]\n",
    ],
)
def test_explicit_uv_project_cannot_silently_fall_back_without_lock(tmp_path, settings):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="science"\nversion="1"\n' + settings
    )
    with pytest.raises(ValueError, match="uv.lock is required"):
        load_uv_project(tmp_path)


def test_plain_legacy_pyproject_without_uv_settings_keeps_manifest_compatibility(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="science"\nversion="1"\ndependencies=["numpy>=2"]\n'
    )
    (tmp_path / "simstack-workflow.toml").write_text(
        '[workflow]\nid="legacy"\nversion="1"\nmodules=["example"]\nruntime_profile="python"\n'
    )
    assert load_uv_project(tmp_path) is None
    assert load_manifest(tmp_path).workflow.dependencies == ["numpy>=2"]
