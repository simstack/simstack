from __future__ import annotations

import json
import subprocess
import sys
import uuid

import pytest

from simstack.workflows.discovery import discover_workflow
from simstack.workflows.manifest import load_manifest
from simstack.workflows.runtime import execute_request
from simstack.util.generate_ui_schema import ui_schema_identity


@pytest.fixture
def workflow_source(tmp_path):
    package = "workflow_" + uuid.uuid4().hex
    root = tmp_path / "snapshot"
    module = root / package
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    (module / "nodes.py").write_text("""from datetime import date
from typing import Annotated
from pydantic import BaseModel, field_validator
from simstack.core.node import node
from simstack.core.simstack_result import SimstackResult
from simstack.models.document import Model, Reference
from simstack.models.artifact_models import ArtifactModel

class Leaf(Model):
    value: int
    content: bytes = b"data"
    day: date = date(2020, 1, 2)

    @field_validator("value")
    @classmethod
    def positive(cls, value):
        if value < 0:
            raise ValueError("value must be positive")
        return value

class Inner(BaseModel):
    values: dict[str, list[Leaf]] = Reference()

class Bundle(Model):
    inner: Inner

@node(version="v1")
def child(value: Leaf, **kwargs) -> Leaf:
    raise RuntimeError("discovery must not execute workflow bodies")

def helper(value):
    return child(value)

@node
async def parent(value: Annotated[Bundle, "public"], **kwargs) -> Bundle:
    helper(value.inner.values["x"][0])
    return value

@node
def multiple(value: Leaf, **kwargs) -> SimstackResult:
    raise RuntimeError("do not execute at upload")

async def collect(arguments):
    return [ArtifactModel(name="actual", data={"value": arguments.result.inner.values["x"][0].value})]
""")
    manifest = root / "simstack-workflow.toml"
    manifest.write_text(f'''[workflow]
id = "org.example.workflows"
version = "1.2.3"
modules = ["{package}.nodes"]
runtime_profile = "science"
dependencies = []

[nodes."{package}.nodes.multiple"]
results = [{{name="first", mapping="{package}.nodes.Leaf"}}, {{name="second", mapping="{package}.nodes.Leaf"}}]

[[artifacts]]
function_mapping="{package}.nodes.collect"
nodes=["{package}.nodes.parent"]
''')
    yield root, package
    sys.path[:] = [path for path in sys.path if path != str(root)]
    for name in list(sys.modules):
        if name == package or name.startswith(package + "."):
            sys.modules.pop(name)


def test_discovery_derives_canonical_schemas_references_and_children(workflow_source):
    root, package = workflow_source
    plan = discover_workflow(root, workflow_revision="a" * 64)
    assert plan["package_id"] == "org.example.workflows"
    assert plan["runtime_profile"] == "science"
    assert {model["mapping"] for model in plan["models"]} == {
        f"{package}.nodes.Leaf",
        f"{package}.nodes.Bundle",
    }
    assert all(model["name"] == model["mapping"] for model in plan["models"])
    parent = next(
        node for node in plan["nodes"] if node["function_mapping"].endswith(".parent")
    )
    assert parent["called_nodes"] == [f"{package}.nodes.child"]
    assert parent["input_mappings"][0]["mapping"] == f"{package}.nodes.Bundle"
    assert len(parent["function_hash"]) == 64
    multiple = next(
        node for node in plan["nodes"] if node["function_mapping"].endswith(".multiple")
    )
    assert [item["name"] for item in multiple["result_mappings"]] == ["first", "second"]
    assert plan["warnings"] == []


def _graph(workflow_source):
    root, package = workflow_source
    plan = discover_workflow(root)
    leaf, bundle = "0" * 23 + "1", "0" * 23 + "2"
    leaf_mapping, bundle_mapping = f"{package}.nodes.Leaf", f"{package}.nodes.Bundle"
    request = {
        "operation": "validate_graph",
        "source_root": str(root),
        "modules": plan["modules"],
        "mappings": plan["models"],
        "documents": [
            {"mapping": leaf_mapping, "id": leaf, "document": {"id": leaf, "value": 3}},
            {
                "mapping": bundle_mapping,
                "id": bundle,
                "document": {"id": bundle, "inner": {"values": {"x": [leaf]}}},
            },
        ],
        "roots": [{"mapping": bundle_mapping, "id": bundle}],
    }
    return request


def test_runtime_hydrates_embedded_reference_containers_and_preserves_binary_dates(
    workflow_source,
):
    request = _graph(workflow_source)
    result = execute_request(request)
    leaf, bundle = result["documents"]
    assert leaf["document"]["content"] == {
        "$binary": {"base64": "ZGF0YQ==", "subType": "00"}
    }
    assert leaf["document"]["day"] == {"$date": "2020-01-02"}
    assert bundle["document"]["inner"]["values"]["x"] == [leaf["id"]]
    assert result["roots"] == request["roots"]


def test_runtime_uses_custom_python_validator_not_only_json_schema(workflow_source):
    request = _graph(workflow_source)
    request["documents"][0]["document"]["value"] = -1
    with pytest.raises(ValueError):
        execute_request(request)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "wrong_id", "unknown_field", "schema"]
)
def test_runtime_rejects_invalid_graph_identity_and_contract(workflow_source, mutation):
    request = _graph(workflow_source)
    if mutation == "missing":
        request["documents"].pop(0)
    elif mutation == "duplicate":
        request["documents"].append(request["documents"][0])
    elif mutation == "wrong_id":
        request["documents"][0]["document"]["id"] = "f" * 24
    elif mutation == "unknown_field":
        request["documents"][0]["document"]["surprise"] = True
    else:
        request["mappings"][0]["json_schema"] = json.dumps({"type": "object"})
    with pytest.raises(ValueError):
        execute_request(request)


@pytest.mark.parametrize(
    "requirement",
    [
        "simstack>=1",
        "PyMongo",
        "motor",
        "odmantic",
        "bson",
        "library @ https://example.org/code.whl",
        "--index-url https://example.org",
    ],
)
def test_manifest_rejects_core_replacement_drivers_and_non_index_dependencies(
    tmp_path, requirement
):
    path = tmp_path / "simstack-workflow.toml"
    path.write_text(f"""[workflow]
id="valid"
version="1"
modules=["nodes"]
runtime_profile="generic"
dependencies=[{json.dumps(requirement)}]
""")
    with pytest.raises(ValueError):
        load_manifest(tmp_path)


def test_manifest_reads_static_project_dependencies_when_omitted(tmp_path):
    (tmp_path / "simstack-workflow.toml").write_text("""[workflow]
id="valid"
version="1"
modules=["nodes"]
runtime_profile="generic"
""")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies=["numpy>=2", "library[extra]==1; python_version > \'3\'"]\n'
    )
    assert load_manifest(tmp_path).workflow.dependencies[0] == "numpy>=2"


@pytest.mark.parametrize("metadata", ["simstack-workflow.toml", "pyproject.toml"])
def test_metadata_read_budget_is_enforced_before_toml_parser(
    monkeypatch, tmp_path, metadata
):
    from simstack.workflows import manifest as module

    ordinary_manifest = '[workflow]\nid="valid"\nversion="1"\nmodules=["nodes"]\nruntime_profile="generic"\n'
    (tmp_path / "simstack-workflow.toml").write_text(ordinary_manifest)
    (tmp_path / metadata).write_bytes(b"#" * 257)
    parsed = []
    original = module.tomllib.loads

    def parse_only_bounded_input(data):
        assert len(data.encode("utf-8")) <= 256
        parsed.append(data)
        return original(data)

    monkeypatch.setattr(module.tomllib, "loads", parse_only_bounded_input)
    with pytest.raises(ValueError, match="configured manifest size"):
        load_manifest(tmp_path, max_bytes=256)
    assert len(parsed) == (0 if metadata == "simstack-workflow.toml" else 1)


def test_manifest_and_static_project_dependencies_within_budget_remain_supported(
    tmp_path,
):
    (tmp_path / "simstack-workflow.toml").write_text(
        '[workflow]\nid="valid"\nversion="1"\nmodules=["nodes"]\nruntime_profile="generic"\n'
    )
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["numpy>=2"]\n')
    manifest = load_manifest(tmp_path, max_bytes=256)
    assert manifest.workflow.id == "valid"
    assert manifest.workflow.dependencies == ["numpy>=2"]


@pytest.mark.parametrize("budget", [None, False, 0, -1, 1.5])
def test_manifest_budget_cannot_be_disabled_by_invalid_values(tmp_path, budget):
    with pytest.raises(ValueError, match="positive integer"):
        load_manifest(tmp_path, max_bytes=budget)


def test_manifest_identity_has_no_ascii_slug_or_semantic_version_policy(tmp_path):
    (tmp_path / "simstack-workflow.toml").write_text("""[workflow]
id="Локальный workflow / example"
version="research prototype"
modules=["пример.вычисления"]
runtime_profile="approved-profile"
dependencies=[]
""")
    manifest = load_manifest(tmp_path)
    assert manifest.workflow.id == "Локальный workflow / example"
    assert manifest.workflow.modules == ["пример.вычисления"]


def test_discovery_dependency_freeze_keeps_effective_overlay_version(
    workflow_source, monkeypatch, tmp_path
):
    from types import SimpleNamespace
    import simstack.workflows.discovery as discovery

    distributions = [
        SimpleNamespace(
            metadata={"Name": name},
            version=version,
            locate_file=lambda name: tmp_path / "operator",
        )
        for name, version in (
            ("Pydantic", "2.13.0"),
            ("pydantic", "2.10.0"),
            ("pip", "25.0"),
        )
    ]
    monkeypatch.setattr(
        discovery.importlib.metadata, "distributions", lambda: distributions
    )
    assert discover_workflow(workflow_source[0])["resolved_dependencies"] == [
        "pydantic==2.13.0"
    ]


def test_uploaded_class_spoofing_core_module_is_not_trusted_provenance(workflow_source):
    root, package = workflow_source
    source = root / package / "nodes.py"
    source.write_text(
        source.read_text()
        + """\nclass Spoofed(Model):
    __module__ = "simstack.models.base_types"
    value: int

@node
def spoofed(value: Spoofed, **kwargs) -> Spoofed:
    return value
"""
    )
    metadata = next(
        item
        for item in discover_workflow(root)["models"]
        if item["mapping"] == "simstack.models.base_types.Spoofed"
    )
    assert metadata["source_kind"] != "core"


def test_discovery_code_hash_and_ast_inspection_are_warnings_not_upload_gates(
    workflow_source, monkeypatch
):
    import simstack.workflows.discovery as discovery

    def unavailable(*args, **kwargs):
        raise ValueError("source not inspectable")

    monkeypatch.setattr(discovery, "_children", unavailable)
    monkeypatch.setattr(discovery, "node_code_version", unavailable)
    result = discover_workflow(workflow_source[0])
    assert len(result["nodes"]) == 3
    assert all(node["function_hash"] is None for node in result["nodes"])
    assert result["warnings"]


def test_runtime_artifact_uses_hydrated_real_model_values(workflow_source):
    request = _graph(workflow_source)
    request["operation"] = "artifact"
    request["function_mapping"] = workflow_source[1] + ".nodes.collect"
    request["arguments"] = {
        "result": request["roots"][0],
        "task_id": "f" * 24,
        "inputs": {},
    }
    result = execute_request(request)
    assert len(result["artifacts"]) == 1
    assert (
        result["artifacts"][0]["mapping"]
        == "simstack.models.artifact_models.ArtifactModel"
    )
    assert result["artifacts"][0]["document"]["data"] == {"value": 3}
    assert "id" in result["artifacts"][0]["document"]


def test_runtime_editor_preview_and_code_use_hydrated_arguments(workflow_source):
    request = _graph(workflow_source)
    request.update(
        operation="artifact_arguments",
        arguments={"result": request["roots"][0], "inputs": {}, "task_id": "f" * 24},
    )
    preview = execute_request(request)["arguments"]
    assert preview["result"]["inner"]["values"]["x"][0]["value"] == 3
    assert preview["result"]["inner"]["values"]["x"][0]["content"] == "<bytes:4>"
    request.update(
        operation="artifact_code",
        function_code="def render(arg):\n    return ArtifactModel(name='editor', data={'value': arg.result.inner.values['x'][0].value})",
    )
    assert execute_request(request)["artifacts"][0]["document"]["data"] == {"value": 3}


def test_runtime_file_backed_outputs_stay_inside_scoped_output_directory(
    workflow_source, tmp_path
):
    from pathlib import Path

    root, package = workflow_source
    source = root / package / "nodes.py"
    source.write_text(
        source.read_text()
        + """\nfrom simstack.core.file_io import current_file_io
from simstack.models.files import FileStack
def collect(arguments):
    path = current_file_io().staging_dir / 'generated.txt'
    path.write_text('durable output')
    file = FileStack.from_local_file(path, in_memory=False)
    return ArtifactModel(name='file-backed', data={'file': file})
"""
    )
    request = _graph(workflow_source)
    files = tmp_path / "render"
    (files / "inputs").mkdir(parents=True)
    (files / "outputs").mkdir()
    request.update(
        operation="artifact",
        function_mapping=package + ".nodes.collect",
        arguments={"result": request["roots"][0], "inputs": {}},
        files_root=str(files),
        file_io={"resource": "server", "max_inline_document_bytes": 1024, "files": []},
    )
    reply = execute_request(request)
    assert len(reply["files"]) == 1
    entry = reply["files"][0]
    assert entry["path"].startswith("outputs/")
    assert (files / entry["path"]).read_text() == "durable output"
    assert Path(entry["document"]["locations"][0]["path"]).parts[0] == "outputs"


def test_same_package_child_artifact_keeps_typed_file_references(workflow_source):
    from simstack.models.files import FileStack
    from simstack.workflows.runtime import _wire_document

    root, package = workflow_source
    source = root / package / "nodes.py"
    source.write_text(
        source.read_text()
        + """\nfrom simstack.models.files import FileStack
class CustomArtifact(ArtifactModel):
    file: FileStack = Reference()
"""
    )
    request = _graph(workflow_source)
    file = FileStack.from_string("child payload", "child.txt")
    file_document = _wire_document(file)
    mapping = package + ".nodes.CustomArtifact"
    child = {"id": "c" * 24, "name": "child", "data": {}, "file": str(file.id)}
    request["documents"].extend(
        [
            {
                "mapping": "simstack.models.files.FileStack",
                "id": str(file.id),
                "document": file_document,
            },
            {"mapping": mapping, "id": child["id"], "document": child},
        ]
    )
    request["roots"].append({"mapping": mapping, "id": child["id"]})
    request.update(
        operation="artifact_files",
        arguments={
            "result": request["roots"][0],
            "inputs": {},
            "child_artifacts": [
                {
                    "mapping": mapping,
                    "artifact_type": "simstack.models.artifact_models.ArtifactModel",
                    "document": child,
                }
            ],
        },
    )
    assert execute_request(request)["files"] == [file_document]


def test_package_public_reexports_keep_actual_canonical_identities(workflow_source):
    root, package = workflow_source
    (root / package / "__init__.py").write_text(
        "from .nodes import Leaf, Bundle, child, parent, multiple, collect\n"
    )
    manifest = root / "simstack-workflow.toml"
    manifest.write_text(
        manifest.read_text().replace(
            f'modules = ["{package}.nodes"]', f'modules = ["{package}"]'
        )
    )
    plan = discover_workflow(root)
    assert {item["function_mapping"] for item in plan["nodes"]} == {
        f"{package}.nodes.child",
        f"{package}.nodes.parent",
        f"{package}.nodes.multiple",
    }
    request = _graph(workflow_source)
    request.update(operation="artifact", function_mapping=f"{package}.nodes.collect")
    request["arguments"] = {"result": request["roots"][0], "inputs": {}}
    assert execute_request(request)["artifacts"][0]["document"]["data"] == {"value": 3}


def test_discovery_optional_ui_hook_failure_is_warning_not_upload_gate(
    workflow_source, monkeypatch
):
    import importlib

    root, package = workflow_source
    discover_workflow(root)
    leaf = importlib.import_module(package + ".nodes").Leaf

    def unavailable(cls):
        raise RuntimeError("UI context unavailable at inspection")

    monkeypatch.setattr(leaf, "ui_schema", classmethod(unavailable), raising=False)
    plan = discover_workflow(root)
    metadata = next(
        item for item in plan["models"] if item["mapping"].endswith(".Leaf")
    )
    assert metadata["ui_schema"] is None
    assert metadata["ui_schema_version"] is None
    assert any(
        "optional UI schema is unavailable" in warning for warning in plan["warnings"]
    )
    assert len(plan["nodes"]) == 3


def test_plain_uploaded_models_have_matching_generated_discovery_and_runtime_ui(
    workflow_source,
):
    root, package = workflow_source
    source = root / package / "nodes.py"
    source.write_text(
        source.read_text()
        + """\nfrom simstack.models.document import EmbeddedModel
from simstack.models.files import FileStack

class EmbeddedFiles(EmbeddedModel):
    attachment: FileStack
    children: list["EmbeddedFiles"] = []

class PlainFiles(Model):
    source: FileStack
    nested: EmbeddedFiles
    optional: FileStack | None = None
    by_name: dict[str, list[FileStack | None]]

@node
def files(value: PlainFiles, **kwargs) -> PlainFiles:
    return value
"""
    )
    plan = discover_workflow(root)
    mapping = f"{package}.nodes.PlainFiles"
    metadata = next(item for item in plan["models"] if item["mapping"] == mapping)
    schema = json.loads(metadata["ui_schema"])
    assert metadata["ui_schema_version"] == ui_schema_identity()
    assert schema["source"]["ui:field"] == "FileField"
    assert schema["nested"]["attachment"]["ui:field"] == "FileField"
    recursive_ui = schema["nested"]["children"]["items"]
    assert recursive_ui["ui:field"] == "RecursiveFormField"
    definition = recursive_ui["ui:options"]["definition"]
    assert schema["ui:definitions"][definition]["attachment"]["ui:field"] == "FileField"
    assert schema["optional"]["ui:field"] == "FileField"
    assert schema["by_name"]["additionalProperties"]["items"]["ui:field"] == "FileField"
    assert "simstack.models.files.FileStack" in {
        item["mapping"] for item in plan["models"]
    }
    runtime = execute_request(
        {
            "operation": "ui_schema",
            "source_root": str(root),
            "modules": plan["modules"],
            "mappings": plan["models"],
            "mapping": mapping,
        }
    )
    assert runtime["ui_schema"] == schema
    assert runtime["json_schema"] == json.loads(metadata["json_schema"])
    assert plan["warnings"] == []


@pytest.mark.parametrize("custom", [{}, {"ui:order": ["value", "id"]}, None])
def test_uploaded_explicit_ui_hooks_match_in_discovery_and_runtime(
    workflow_source, monkeypatch, custom
):
    import importlib

    root, package = workflow_source
    discover_workflow(root)
    leaf = importlib.import_module(package + ".nodes").Leaf
    monkeypatch.setattr(
        leaf, "ui_schema", classmethod(lambda cls: custom), raising=False
    )
    plan = discover_workflow(root)
    mapping = f"{package}.nodes.Leaf"
    metadata = next(item for item in plan["models"] if item["mapping"] == mapping)
    assert (
        json.loads(metadata["ui_schema"]) if metadata["ui_schema"] is not None else None
    ) == custom
    assert metadata["ui_schema_version"] == ui_schema_identity()
    runtime = execute_request(
        {
            "operation": "ui_schema",
            "source_root": str(root),
            "modules": plan["modules"],
            "mappings": plan["models"],
            "mapping": mapping,
        }
    )
    assert runtime["ui_schema"] == custom


@pytest.mark.parametrize("custom", [[], {"value": float("nan")}])
def test_malformed_uploaded_ui_hook_warns_at_discovery_and_is_rejected_at_runtime(
    workflow_source, monkeypatch, custom
):
    import importlib

    root, package = workflow_source
    discover_workflow(root)
    leaf = importlib.import_module(package + ".nodes").Leaf
    monkeypatch.setattr(
        leaf, "ui_schema", classmethod(lambda cls: custom), raising=False
    )
    plan = discover_workflow(root)
    mapping = f"{package}.nodes.Leaf"
    metadata = next(item for item in plan["models"] if item["mapping"] == mapping)
    assert metadata["ui_schema"] is None
    assert metadata["ui_schema_version"] is None
    assert any(
        "optional UI schema is unavailable" in warning for warning in plan["warnings"]
    )
    with pytest.raises(ValueError):
        execute_request(
            {
                "operation": "ui_schema",
                "source_root": str(root),
                "modules": plan["modules"],
                "mappings": plan["models"],
                "mapping": mapping,
            }
        )


def test_custom_artifact_subclass_preserves_fields_and_uses_native_collection(
    workflow_source, monkeypatch
):
    import importlib
    from simstack.models.charts_artifact import ChartArtifactModel

    root, package = workflow_source
    source = root / package / "nodes.py"
    source.write_text(
        source.read_text()
        + """\nfrom simstack.models.charts_artifact import ChartArtifactModel
class CustomChart(ChartArtifactModel):
    provenance: bytes = b"source"

async def collect(arguments):
    return CustomChart(name="custom", title={"text": "Custom"}, data=[{"x": 1}])
"""
    )
    request = _graph(workflow_source)
    metadata = next(
        item for item in request["mappings"] if item["mapping"].endswith(".CustomChart")
    )
    assert metadata["collection_name"] == ChartArtifactModel.__collection__
    actual = importlib.import_module(package + ".nodes").CustomChart
    assert actual.__collection__ != ChartArtifactModel.__collection__
    request.update(operation="artifact", function_mapping=package + ".nodes.collect")
    request["arguments"] = {"result": request["roots"][0], "inputs": {}}
    artifact = execute_request(request)["artifacts"][0]
    assert artifact["mapping"] == package + ".nodes.CustomChart"
    assert (
        artifact["artifact_type"]
        == "simstack.models.charts_artifact.ChartArtifactModel"
    )
    assert artifact["collection_name"] == ChartArtifactModel.__collection__
    assert artifact["document"]["provenance"] == {
        "$binary": {"base64": "c291cmNl", "subType": "00"}
    }


def test_runtime_file_collector_get_uses_only_explicit_staging(
    workflow_source, tmp_path
):
    import hashlib
    from simstack.models.files import FileStack
    from simstack.workflows.runtime import _wire_document

    root, package = workflow_source
    source = root / package / "nodes.py"
    source.write_text(
        source.read_text()
        + """\nfrom simstack.models.files import FileStack
class FileBundle(Model):
    stack: FileStack = Reference()

async def collect(arguments):
    path = arguments.result.stack.get()
    generated = FileStack.from_string("generated", "output.txt")
    return ArtifactModel(name="file", data={"input": path.read_text(), "output": generated.get().read_text()})
"""
    )
    plan = discover_workflow(root)
    stack = FileStack.from_string("staged", "input.txt")
    document = _wire_document(stack)
    bundle = "b" * 24
    identity = {"mapping": package + ".nodes.FileBundle", "id": bundle}
    request = {
        "operation": "artifact_files",
        "source_root": str(root),
        "mappings": plan["models"],
        "roots": [identity],
        "documents": [
            {
                "mapping": "simstack.models.files.FileStack",
                "id": str(stack.id),
                "document": document,
            },
            {
                **identity,
                "document": {"id": bundle, "stack": str(stack.id)},
            },
        ],
        "function_mapping": package + ".nodes.collect",
        "arguments": {"result": identity, "inputs": {}},
    }
    assert execute_request(request) == {"files": [document]}
    files_root = tmp_path / "artifact-requests" / "render"
    (files_root / "inputs").mkdir(parents=True)
    (files_root / "outputs").mkdir()
    staged = files_root / "inputs" / "input.txt"
    staged.write_text("staged")
    request.update(
        operation="artifact",
        files_root=str(files_root),
        file_io={
            "resource": "server",
            "max_inline_document_bytes": 15 * 1024**2,
            "files": [
                {
                    "id": str(stack.id),
                    "path": "inputs/input.txt",
                    "size_bytes": 6,
                    "checksum_sha256": hashlib.sha256(b"staged").hexdigest(),
                    "identity": hashlib.sha256(
                        json.dumps(
                            document, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                }
            ],
        },
    )
    assert execute_request(request)["artifacts"][0]["document"]["data"] == {
        "input": "staged",
        "output": "generated",
    }
    staged.write_text("broken")
    with pytest.raises(ValueError, match="content changed"):
        execute_request(request)


def test_runtime_stdio_has_exact_success_and_error_envelopes(workflow_source):
    source, _ = workflow_source
    success = subprocess.run(
        [sys.executable, "-m", "simstack.workflows.runtime"],
        input=json.dumps({"operation": "discover", "source_root": str(source)}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert success.returncode == 0, success.stderr
    assert json.loads(success.stdout)["ok"] is True
    failure = subprocess.run(
        [sys.executable, "-m", "simstack.workflows.runtime"],
        input=json.dumps({"operation": "unknown"}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert failure.returncode == 1
    reply = json.loads(failure.stdout)
    assert reply["ok"] is False
    assert reply["error"]["type"] == "ValueError"
