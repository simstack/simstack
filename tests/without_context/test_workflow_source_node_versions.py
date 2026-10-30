from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import uuid
import zipfile
from types import SimpleNamespace

import pytest

from simstack.core.node_code_version import node_code_version
from simstack.core.runner_execution import mappings
from simstack.core.runner_execution.errors import RunnerTaskPayloadError
from simstack.runner_runtime.workflow_installation import installation_root
from simstack.workflows.discovery import discover_workflow, effective_node_version
from simstack.workflows.snapshot import tree_digest


def _wheel(path, name, module, code):
    info = name + "-1.0.0.dist-info"
    files = {
        module + ".py": code.encode(),
        info
        + "/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n".encode(),
        info
        + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: source-version-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        info + "/entry_points.txt": f"[simstack.modules]\nnodes = {module}\n".encode(),
    }
    record = io.StringIO()
    writer = csv.writer(record)
    for filename, content in sorted(files.items()):
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .rstrip(b"=")
            .decode()
        )
        writer.writerow([filename, "sha256=" + digest, str(len(content))])
    writer.writerow([info + "/RECORD", "", ""])
    files[info + "/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w") as wheel:
        for filename, content in files.items():
            wheel.writestr(filename, content)


@pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is required to install a real wheel"
)
def test_catalogue_and_worker_share_actual_locked_distribution_node_versions(
    tmp_path, monkeypatch
):
    unique = uuid.uuid4().hex
    dependency_name = "version_dependency_" + unique
    dependency_module = "version_nodes_" + unique
    root_module = "version_root_" + unique
    workdir = tmp_path / "runner"
    identity, revision = "1" * 24, "a" * 64
    source = installation_root(workdir, identity, revision) / "source"
    dependency = source / ".simstack/sources" / dependency_name
    dependency.mkdir(parents=True)
    dependency_code = """from simstack.core.node import node
from simstack.models.document import Model
class Value(Model):
    number: int
@node
def child(value: Value) -> Value:
    return value
@node(version="scientific-v2")
def explicit(value: Value) -> Value:
    return value
"""
    declaration = f'''[project]
name="{dependency_name}"
version="1.0.0"
dependencies=[]
[project.entry-points."simstack.modules"]
nodes="{dependency_module}"
'''
    (dependency / "pyproject.toml").write_text(declaration)
    (dependency / (dependency_module + ".py")).write_text(dependency_code)
    wheel = tmp_path / (dependency_name + "-1.0.0-py3-none-any.whl")
    _wheel(wheel, dependency_name, dependency_module, dependency_code)
    target = tmp_path / "installed"
    subprocess.run(
        [
            "uv",
            "--no-config",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(target),
            str(wheel),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.syspath_prepend(str(target))
    (source / "pyproject.toml").write_text(
        f'[project]\nname="version-root"\nversion="7.0.0"\ndependencies=["{dependency_name}"]\n'
    )
    (source / (root_module + ".py")).write_text(f"""from simstack.core.node import node
from {dependency_module} import Value, child
@node
def parent(value: Value) -> Value:
    return child(value)
""")
    (source / "simstack-workflow.toml").write_text(f'''[workflow]
id="test.locked-node-versions"
version="7.0.0"
modules=["{root_module}"]
runtime_profile="python"
''')
    path = dependency.relative_to(source).as_posix()
    (source / "uv.lock").write_text(f'''version=1
requires-python=">=3.12"
[[package]]
name="version-root"
version="7.0.0"
source={{virtual="."}}
[[package]]
name="{dependency_name}"
version="1.0.0"
source={{directory="{path}"}}
''')
    metadata = {
        "version": 1,
        "sources": [
            {
                "name": dependency_name.replace("_", "-"),
                "version": "1.0.0",
                "path": path,
                "source": {"directory": "../library"},
                "tree_sha256": tree_digest(dependency),
            }
        ],
    }
    (source / ".simstack/snapshot.json").write_text(json.dumps(metadata))
    plan = discover_workflow(source, workflow_revision=revision)
    nodes = {item["function_mapping"]: item for item in plan["nodes"]}
    assert nodes[root_module + ".parent"]["version"] == "7.0.0"
    assert nodes[dependency_module + ".child"]["version"] == "1.0.0"
    assert nodes[dependency_module + ".explicit"]["version"] == "scientific-v2"
    assert sys.modules[root_module].Value is sys.modules[dependency_module].Value
    assert (
        len(
            [
                item
                for item in plan["models"]
                if item["mapping"] == dependency_module + ".Value"
            ]
        )
        == 1
    )
    monkeypatch.setattr(
        mappings, "context", SimpleNamespace(config=SimpleNamespace(workdir=workdir))
    )
    for name in (
        root_module + ".parent",
        dependency_module + ".child",
        dependency_module + ".explicit",
    ):
        function = mappings._import_path(name)._inner
        registry = SimpleNamespace(
            name=name,
            func_mapping=name,
            workflow_revision=revision,
            workflow_installation_id=identity,
            version=nodes[name]["version"],
            function_hash=node_code_version(function, workflow_revision=revision),
        )
        model = SimpleNamespace(function_mapping=name, workflow_revision=revision)
        assert mappings._function_for_registry(registry, node_model=model) is function
        registry.version = "incorrect-version"
        with pytest.raises(
            RunnerTaskPayloadError, match="Declared node version mismatch"
        ):
            mappings._function_for_registry(registry, node_model=model)
    (source / "uv.lock").unlink()
    assert effective_node_version(sys.modules[root_module].parent, source) is None
