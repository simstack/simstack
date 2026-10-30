from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
import tomllib
import uuid
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest

from simstack.workflows.discovery import (
    discover_workflow,
    exported_function,
    load_modules,
)
from simstack.workflows.snapshot import build_snapshot, read_snapshot, tree_digest


def _run(command, cwd):
    return subprocess.check_output(
        command, cwd=cwd, text=True, stderr=subprocess.STDOUT
    ).strip()


def _git(command, cwd):
    return _run(
        [
            "git",
            "-c",
            "user.name=Snapshot Test",
            "-c",
            "user.email=snapshot@example.invalid",
            *command,
        ],
        cwd,
    )


def _project(root, name):
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname="{name}"\nversion="1.0.0"\nrequires-python=">=3.12"\ndependencies=[]\n'
    )
    (root / "module.py").write_text("VALUE = 42\n")


@pytest.mark.skipif(
    shutil.which("uv") is None or shutil.which("git") is None,
    reason="uv and Git integration tools required",
)
def test_uv_snapshot_embeds_exact_git_and_local_sources(tmp_path):
    library = tmp_path / "git-library"
    _project(library, "snapshot-git-library")
    _git(["init", "--quiet"], library)
    _git(["add", "."], library)
    _git(["commit", "--quiet", "-m", "Initial library"], library)
    commit = _git(["rev-parse", "HEAD"], library)
    local = tmp_path / "local-library"
    _project(local, "snapshot-local-library")
    root = tmp_path / "project"
    _project(root, "snapshot-main")
    (root / "pyproject.toml").write_text(f'''[project]
name="snapshot-main"
version="1.0.0"
requires-python=">=3.12"
dependencies=["snapshot-git-library", "snapshot-local-library"]
[tool.uv.sources]
snapshot_git_library={{git="{library.as_uri()}", rev="{commit}"}}
snapshot_local_library={{path="../local-library"}}
''')
    (root / "simstack-workflow.toml").write_text(
        '[workflow]\nid="test.snapshot"\nversion="1"\nmodules=["module"]\nruntime_profile="python"\n'
    )
    _run(["uv", "lock", "--python", sys.executable], root)
    original_lock = (root / "uv.lock").read_bytes()
    (root / ".env").write_text("SECRET=do-not-package\n")
    (root / "config.toml").write_text("private=true\n")
    _run(["uv", "venv", ".venv", "--python", sys.executable], root)
    (root / ".venv" / "ignore").write_text("not source")
    archive = tmp_path / "workflow.zip"
    result = build_snapshot(root, archive, python=sys.executable)
    assert result["sha256"]
    with zipfile.ZipFile(archive) as package:
        assert "simstack-workflow.toml" in package.namelist()
        assert not any(
            ".git/" in name or ".venv/" in name for name in package.namelist()
        )
        assert (
            ".env" not in package.namelist() and "config.toml" not in package.namelist()
        )
        assert package.read(".simstack/original/uv.lock") == original_lock
        metadata = json.loads(package.read(".simstack/snapshot.json"))
        assert {item["name"] for item in metadata["sources"]} == {
            "snapshot-git-library",
            "snapshot-local-library",
        }
        git = next(
            item
            for item in metadata["sources"]
            if item["name"] == "snapshot-git-library"
        )
        assert git["source"]["git"].endswith("#" + commit)
        assert package.read(git["path"] + "/module.py") == b"VALUE = 42\n"
        lock = tomllib.loads(package.read("uv.lock").decode())
        assert all("git" not in item["source"] for item in lock["package"])
    assert (root / "uv.lock").read_bytes() == original_lock


def _discovery_fixture(tmp_path, monkeypatch):
    unique = uuid.uuid4().hex
    root = tmp_path / "snapshot"
    root.mkdir()
    package_name = "locked_source_" + unique
    source = root / ".simstack/sources" / package_name
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text(f'''[project]
name="{package_name}"
version="1.2.3"
[project.entry-points."simstack.modules"]
nodes="{package_name}"
''')
    installed = tmp_path / "installed"
    installed.mkdir()
    module = installed / (package_name + ".py")
    module.write_text("""from simstack.models.document import Model
from simstack.core.node import node
class Value(Model):
    number: int
@node
def child(value: Value) -> Value:
    raise RuntimeError("Discovery must not run nodes")
""")
    main_name = "main_" + unique
    (root / (main_name + ".py")).write_text(f"""from {package_name} import Value, child
from simstack.core.node import node
@node
def parent(value: Value) -> Value:
    return child(value)
""")
    (root / "simstack-workflow.toml").write_text(f'''[workflow]
id="test.source-discovery"
version="1"
modules=["{main_name}"]
runtime_profile="python"
dependencies=[]
''')
    metadata = {
        "version": 1,
        "sources": [
            {
                "name": package_name.replace("_", "-"),
                "version": "1.2.3",
                "path": source.relative_to(root).as_posix(),
                "source": {"git": "https://example.invalid/repository.git#" + "a" * 40},
                "tree_sha256": tree_digest(source),
            }
        ],
    }
    (root / ".simstack/snapshot.json").write_text(json.dumps(metadata))

    class Distribution:
        metadata: ClassVar[dict] = {"Name": package_name}
        version = "1.2.3"
        files: ClassVar[list] = [Path(module.name)]

        def locate_file(self, item):
            return installed / item

    original = importlib.metadata.distribution
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: (
            Distribution() if name == package_name.replace("_", "-") else original(name)
        ),
    )
    monkeypatch.syspath_prepend(str(installed))
    return root, source, package_name, main_name, Distribution


def test_discovery_registers_pinned_dependency_nodes_without_copying_class_identity(
    tmp_path, monkeypatch
):
    root, _, dependency, main, _ = _discovery_fixture(tmp_path, monkeypatch)
    plan = discover_workflow(root, workflow_revision="a" * 64)
    nodes = {item["function_mapping"]: item for item in plan["nodes"]}
    assert set(nodes) == {main + ".parent", dependency + ".child"}
    assert nodes[main + ".parent"]["called_nodes"] == [dependency + ".child"]
    assert nodes[dependency + ".child"]["version"] == "1.2.3"
    models = [
        item for item in plan["models"] if item["mapping"] == dependency + ".Value"
    ]
    assert len(models) == 1 and models[0]["source_kind"] == "dependency"
    modules = load_modules(root, [main])
    assert (
        exported_function(modules, dependency + ".child")
        is sys.modules[dependency].child
    )


def test_changed_dependency_source_rejected_before_discovery(tmp_path, monkeypatch):
    root, source, _, _, _ = _discovery_fixture(tmp_path, monkeypatch)
    (source / "pyproject.toml").write_text("tampered=true\n")
    with pytest.raises(ValueError, match="tree changed"):
        discover_workflow(root)


def test_installed_dependency_must_own_its_export_module(tmp_path, monkeypatch):
    root, _, _, _, distribution = _discovery_fixture(tmp_path, monkeypatch)
    distribution.files = []
    with pytest.raises(ValueError, match="immutable source package"):
        discover_workflow(root)


def test_snapshot_rejects_source_path_escape(tmp_path):
    root = tmp_path / "snapshot"
    (root / ".simstack").mkdir(parents=True)
    (root / ".simstack/snapshot.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "name": "escape",
                        "version": "1",
                        "path": "../elsewhere",
                        "source": {},
                        "tree_sha256": "a" * 64,
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="escapes"):
        read_snapshot(root)


@pytest.mark.skipif(
    shutil.which("uv") is None or shutil.which("git") is None,
    reason="uv and Git integration tools required",
)
def test_snapshot_materializes_recursive_submodules_at_gitlink_commits(tmp_path):
    leaf = tmp_path / "leaf"
    _project(leaf, "snapshot-leaf")
    _git(["init", "--quiet"], leaf)
    _git(["add", "."], leaf)
    _git(["commit", "--quiet", "-m", "Leaf source"], leaf)
    middle = tmp_path / "middle"
    _project(middle, "snapshot-middle")
    _git(["init", "--quiet"], middle)
    _git(
        [
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            leaf.as_uri(),
            "nested/leaf",
        ],
        middle,
    )
    _git(["add", "."], middle)
    _git(["commit", "--quiet", "-m", "Pin nested submodule"], middle)
    root = tmp_path / "root"
    _project(root, "snapshot-root")
    (root / "simstack-workflow.toml").write_text(
        '[workflow]\nid="test.recursive"\nversion="1"\nmodules=["module"]\nruntime_profile="python"\n'
    )
    _git(["init", "--quiet"], root)
    _git(
        [
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            middle.as_uri(),
            "vendor/middle",
        ],
        root,
    )
    _git(["add", "."], root)
    _git(["commit", "--quiet", "-m", "Pin project submodule"], root)
    # The nested leaf is deliberately uninitialized in the author's checkout.
    assert not (root / "vendor/middle/nested/leaf/module.py").exists()
    _run(["uv", "lock", "--python", sys.executable], root)
    output = tmp_path / "recursive.zip"
    build_snapshot(root, output, python=sys.executable)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("vendor/middle/nested/leaf/module.py") == b"VALUE = 42\n"
        assert not any(
            part == ".git" for name in archive.namelist() for part in Path(name).parts
        )


def test_uv_discovery_reports_active_installed_versions_and_source_identity(
    tmp_path, monkeypatch
):
    root, source, package_name, _, dependency_distribution = _discovery_fixture(
        tmp_path, monkeypatch
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname="snapshot-main"\nversion="1.0"\ndependencies=["{package_name}", "marker-package"]\n'
    )
    source_path = source.relative_to(root).as_posix()
    marker = json.dumps(f"sys_platform == '{sys.platform}'")
    other_marker = json.dumps(f"sys_platform != '{sys.platform}'")
    (root / "uv.lock").write_text(f'''version=1
requires-python=">=3.12"
[[package]]
name="snapshot-main"
version="1.0"
source={{virtual="."}}
[[package]]
name="{package_name}"
version="1.2.3"
source={{directory="{source_path}"}}
[[package]]
name="marker-package"
version="1.0"
source={{registry="https://pypi.org/simple"}}
resolution-markers=[{marker}]
[[package]]
name="marker-package"
version="2.0"
source={{registry="https://pypi.org/simple"}}
resolution-markers=[{other_marker}]
''')

    class IndexDistribution:
        metadata: ClassVar[dict] = {"Name": "marker-package"}
        version = "1.0"

        def locate_file(self, item):
            return tmp_path / "installed" / item

    monkeypatch.setattr(
        importlib.metadata,
        "distributions",
        lambda: [IndexDistribution(), dependency_distribution()],
    )
    plan = discover_workflow(root)
    assert plan["dependency_mode"] == "uv"
    assert "marker-package==1.0" in plan["resolved_dependencies"]
    assert "marker-package==2.0" not in plan["resolved_dependencies"]
    assert len(plan["project_identity"]["lock_sha256"]) == 64
    assert plan["source_dependencies"][0]["source"]["git"].endswith("a" * 40)
