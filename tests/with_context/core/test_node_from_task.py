"""Runner reconstructs canonical task DTOs; admission/repair belongs to Server."""

from server import storage_criteria
from server import storage_schema
from simstack.runner_runtime.launch_claims import acquire_work
from tests.support.domain_files import staged_file
import pytest
import pytest_asyncio
from uuid import uuid4
from simstack.models.document import ObjectId
from simstack.core.context import context
from simstack.core.node import node, node_from_task, compute_arg_hash
from simstack.models import FloatData, NodeModel, NodeRegistry, Parameters
from simstack.models.named_data_reference import NamedDataReference
from simstack.models.pandas_model import PandasModel
from simstack.core.definitions import TaskStatus
from simstack.runner_runtime.launch_claims import task_execution, current_execution

CURRENT_MODULE = __name__


@node()
def helper_node_func(args: FloatData, **kwargs) -> FloatData:
    return FloatData(value=args.value + 1)


@pytest_asyncio.fixture(autouse=True)
async def setup_helper_node_model(server_storage, initialized_context):
    existing = await server_storage.find_one(NodeModel, storage_criteria.field(NodeModel, 'name') == "helper_node_func")
    mapping = NodeModel(name="helper_node_func", function_mapping=f"{CURRENT_MODULE}.helper_node_func",
        input_mappings=[], default_parameters=Parameters())
    if existing:
        mapping.id = existing.id
    await server_storage.save(mapping)
    await context.refresh_mappings()
    return mapping


async def prepare_task(entry):
    if entry.status == TaskStatus.SUBMITTED:
        tasks = await acquire_work(context.server, capacity=100)
        entry = next(item for item in tasks if item.id == entry.id)
    with task_execution(entry):
        assert current_execution()["start_id"]
    return entry


async def save_task_with_inputs(server_storage, task):
    """Use Server admission's immutable graph before seeding the task DTO."""
    from server.task_submission import capture_submission_inputs
    document = {"_id": task.id, "repository_revision": task.repository_revision,
        "input_references": [reference.model_dump(mode="json") for reference in task.input_references]}
    await capture_submission_inputs(server_storage, server_storage.core_engine.user, document)
    await server_storage.save(task)
    generation = getattr(task, "_execution_generation", None) or str(uuid4())
    await server_storage.get_collection(NodeRegistry).update_one({"_id": task.id}, {
        "$set": {"_runner_input_snapshot": document["_runner_input_snapshot"],
                 "_runner_generation": generation}})
    object.__setattr__(task, "_execution_generation", generation)


async def canonical_task(server_storage, *, status=TaskStatus.SUBMITTED, version=None, mapping=None, value=10.0):
    data = await context.server.values.publish_value(FloatData(value=value))
    task = NodeRegistry(name="helper_node_func", status=status,
        input_references=[NamedDataReference.from_variable(data, variable_name="args")],
        function_hash="", arg_hash=compute_arg_hash([data]), version=version,
        func_mapping=mapping or f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(resource="test"))
    await save_task_with_inputs(server_storage, task)
    return await prepare_task(task) if status == TaskStatus.SUBMITTED else task


@pytest.mark.asyncio
async def test_node_from_task_basic(server_storage):
    task = await canonical_task(server_storage)
    reconstructed = await node_from_task(task)
    assert reconstructed.name == "helper_node_func"
    assert reconstructed._args[0].value == 10.0
    assert reconstructed.registry_entry.id == task.id
    assert reconstructed.registry_entry.arg_hash == task.arg_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("historical_hash", ["", "NOT INITIALIZED", "older-body-hash"])
async def test_completed_snapshot_is_not_rewritten_by_runner(server_storage, historical_hash):
    task = await canonical_task(server_storage, status=TaskStatus.COMPLETED)
    task.function_hash = historical_hash
    await server_storage.save(task)
    assert await node_from_task(task) is not None
    saved = await context.server.tasks.get_task(task.id)
    assert saved.function_hash == historical_hash
    assert saved.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_completed_result_keeps_original_declared_version(server_storage, monkeypatch):
    task = await canonical_task(server_storage, status=TaskStatus.COMPLETED, version="original-version")
    monkeypatch.setattr(helper_node_func._inner, "_node_version", "worker-v2")
    assert await node_from_task(task) is not None
    saved = await context.server.tasks.get_task(task.id)
    assert saved.version == "original-version" and saved.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_pending_version_mismatch_records_original_attempt_failure(server_storage, monkeypatch):
    task = await canonical_task(server_storage, version="original-version")
    monkeypatch.setattr(helper_node_func._inner, "_node_version", "worker-v2")
    assert await node_from_task(task) is None
    saved = await context.server.tasks.get_task(task.id)
    assert saved.status == TaskStatus.FAILED
    assert "Declared node version mismatch" in saved.error
    assert saved.version == "original-version"


@pytest.mark.asyncio
async def test_same_declared_version_reconstructs_and_executes(server_storage, monkeypatch):
    monkeypatch.setattr(helper_node_func._inner, "_node_version", "release-3")
    task = await canonical_task(server_storage, version="release-3")
    from simstack.core.services.node_execution_service import run_node_from_registry_with_outcome
    assert (await run_node_from_registry_with_outcome(task)).success
    saved = await context.server.tasks.get_task(task.id)
    assert saved.version == "release-3" and saved.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_node_from_task_imports_through_explicit_catalogue_client(server_storage):
    task = await canonical_task(server_storage, value=5.0)
    context._node_mappings = None
    reconstructed = await node_from_task(task)
    assert reconstructed._args[0].value == 5.0


@pytest.mark.asyncio
async def test_node_from_task_invalid_mapping_is_fenced_failure(server_storage):
    task = await canonical_task(server_storage, mapping="non_existent_module.func")
    assert await node_from_task(task) is None
    assert (await context.server.tasks.get_task(task.id)).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_node_from_task_does_not_adopt_other_active_snapshot(server_storage):
    first = await canonical_task(server_storage)
    second = await canonical_task(server_storage)
    reconstructed = await node_from_task(second)
    assert reconstructed.registry_entry.id == second.id
    assert (await context.server.tasks.get_task(first.id)).status == TaskStatus.RETRIEVED
    assert second.id not in (await context.server.tasks.get_task(first.id)).parent_ids


@pytest.mark.parametrize("script,include_node_name", [
    ("scripts/generate_tests.py", True),
    ("tests/extract_node_data.py", False),
])
@pytest.mark.asyncio
async def test_standalone_task_export_reads_actual_domain_api(
    server_storage, domain_bridge, tmp_path, script, include_node_name,
):
    import importlib.util
    import json
    from pathlib import Path

    value = await context.server.values.publish_value(FloatData(value=42))
    reference = NamedDataReference(variable_name="result",
        variable_mapping="simstack.models.base_types.FloatData", reference=value.id)
    task = NodeRegistry(name="export_probe", status=TaskStatus.SUBMITTED,
        function_hash="", arg_hash="export-input", func_mapping="fixture.export_probe",
        parameters=Parameters(resource="test"), input_references=[reference],
        results_references=[reference])
    await save_task_with_inputs(server_storage, task)
    task = await prepare_task(task)
    with task_execution(task):
        await context.server.tasks.start_attempt(task)
        _, publication = await context.server.values.publish_task_outputs([value])
    object.__setattr__(task, "_output_publication", publication)
    task = await context.server.tasks.complete_attempt(task, outputs=[reference], return_kind="model")
    value.value = 99
    await context.server.values.publish_value(value)
    source = Path(context.config.workdir) / task.name / str(task.id)
    source.mkdir(parents=True)
    (source / "output.dat").write_bytes(b"saved scientific output")
    path = Path(__file__).resolve().parents[3] / script
    spec = importlib.util.spec_from_file_location("domain_task_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    await module.extract_node_data(str(task.id), tmp_path / "export")

    target = tmp_path / "export"
    if include_node_name:
        target /= task.name
    target /= task.arg_hash
    assert json.loads((target / "inputs.json").read_text())[0]["data"]["value"] == 42
    assert json.loads((target / "results.json").read_text())[0]["name"] == "result"
    assert json.loads((target / "results.json").read_text())[0]["data"]["value"] == 42
    assert (target / "output.dat").read_bytes() == b"saved scientific output"
    assert (target / f"{task.id}.json").is_file()
    assert any(method == "GET" and route.endswith(f"/tasks/{task.id}")
               for method, route, body in domain_bridge.api.requests.transport.calls)


@pytest.mark.asyncio
async def test_node_from_task_hydrates_embedded_filestack(server_storage,
    initialized_context, setup_helper_node_model
):
    canonical = staged_file(
        name="D3_unexpected.xyz",
        size=5557,
        is_hashable=False,
        in_memory=True,
        content=b"canonical-content",
    )
    canonical = await context.server.values.publish_value(canonical)

    input_id = ObjectId()
    collection = server_storage.raw_database[storage_schema.collection_for_model(PandasModel)]
    await collection.insert_one(
        {
            "_id": input_id,
            "field_name": f"filestack_hydration_{input_id}",
            "content_": b"",
            "file_stack": {
                "name": canonical.name,
                "size": canonical.size,
                "is_hashable": False,
                "hash": None,
                "in_memory": False,
                "content": None,
                "locations": [],
                "id": str(canonical.id),
            },
        }
    )

    registry_entry = NodeRegistry(
        name="helper_node_func",
        status=TaskStatus.SUBMITTED,
        input_references=[
            NamedDataReference(
                variable_name="args",
                variable_mapping="simstack.models.pandas_model.PandasModel",
                reference=input_id,
            )
        ],
        function_hash="",
        arg_hash="canonical-input-hash",
        func_mapping=f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(),
    )
    await save_task_with_inputs(server_storage, registry_entry)

    try:
        registry_entry = await prepare_task(registry_entry)
        reconstructed_node = await node_from_task(registry_entry)

        assert reconstructed_node is not None
        hydrated = reconstructed_node._args[0].file_stack
        assert hydrated is not None
        assert hydrated.id == canonical.id
        assert hydrated.in_memory is False
        assert hydrated.content == canonical.content
        assert [(item.id, item.path, item.checksum_sha256) for item in hydrated.locations] == [
            (item.id, item.path, item.checksum_sha256) for item in canonical.locations]

        stored = await collection.find_one({"_id": input_id})
        assert stored["file_stack"]["in_memory"] is False
        assert stored["file_stack"]["content"] is None
        assert stored["file_stack"]["locations"] == []
    finally:
        await server_storage.delete(registry_entry)
        await collection.delete_one({"_id": input_id})
        await server_storage.delete(canonical)


@pytest.mark.asyncio
async def test_node_from_task_keeps_complete_embedded_filestack(server_storage,
    initialized_context, setup_helper_node_model, tmp_path
):
    embedded_id = ObjectId()
    embedded_file = staged_file(name="embedded.xyz", size=7, content=b"content")
    embedded_doc = embedded_file.model_dump()
    embedded_doc["id"] = str(embedded_id)
    input_id = ObjectId()
    collection = server_storage.raw_database[storage_schema.collection_for_model(PandasModel)]
    await collection.insert_one(
        {
            "_id": input_id,
            "field_name": f"complete_embedded_filestack_{input_id}",
            "content_": b"",
            "file_stack": embedded_doc,
        }
    )

    registry_entry = NodeRegistry(
        name="helper_node_func",
        status=TaskStatus.SUBMITTED,
        input_references=[
            NamedDataReference(
                variable_name="args",
                variable_mapping="simstack.models.pandas_model.PandasModel",
                reference=input_id,
            )
        ],
        function_hash="",
        arg_hash="canonical-input-hash",
        func_mapping=f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(),
    )
    await save_task_with_inputs(server_storage, registry_entry)

    try:
        registry_entry = await prepare_task(registry_entry)
        reconstructed_node = await node_from_task(registry_entry)

        assert reconstructed_node is not None
        embedded = reconstructed_node._args[0].file_stack
        assert embedded is not None
        assert embedded.id == embedded_id
        assert embedded.content is None
        assert embedded.get(tmp_path / "embedded").read_bytes() == b"content"
    finally:
        await server_storage.delete(registry_entry)
        await collection.delete_one({"_id": input_id})


@pytest.mark.asyncio
async def test_task_input_admission_rejects_missing_embedded_filestack(server_storage,
    initialized_context, setup_helper_node_model
):
    from fastapi import HTTPException
    missing_id = ObjectId()
    input_id = ObjectId()
    collection = server_storage.raw_database[storage_schema.collection_for_model(PandasModel)]
    await collection.insert_one(
        {
            "_id": input_id,
            "field_name": f"missing_embedded_filestack_{input_id}",
            "content_": b"",
            "file_stack": {
                "name": "missing.xyz",
                "size": 7,
                "is_hashable": False,
                "hash": None,
                "in_memory": False,
                "content": None,
                "locations": [],
                "id": str(missing_id),
            },
        }
    )

    registry_entry = NodeRegistry(
        name="helper_node_func",
        status=TaskStatus.SUBMITTED,
        input_references=[
            NamedDataReference(
                variable_name="args",
                variable_mapping="simstack.models.pandas_model.PandasModel",
                reference=input_id,
            )
        ],
        function_hash="",
        arg_hash="canonical-input-hash",
        func_mapping=f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(),
    )
    try:
        # Missing bytes/metadata are rejected while capturing immutable inputs,
        # before an invalid task can be admitted to the Runner's queue.
        with pytest.raises(HTTPException) as rejected:
            await save_task_with_inputs(server_storage, registry_entry)
        assert rejected.value.status_code == 404
        assert rejected.value.detail == "Scientific value does not exist"
        assert await server_storage.get_collection(NodeRegistry).find_one({"_id": registry_entry.id}) is None
    finally:
        await collection.delete_one({"_id": input_id})
