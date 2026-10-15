"""Execution writes retain user edits, stable links, and previous artifacts."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.node_persistence import Node
from simstack.core.recompute_artifacts import recompute_artifacts
from simstack.core.simstack_result import SimstackResult
from simstack.models import NodeRegistry
from simstack.models.artifact_models import ArtifactMapping, ArtifactModel
from simstack.models.document import ObjectId
from simstack.models.file_list import FileList
from simstack.models.files import FileStack
from simstack.models.parameters import Parameters, Resource
from simstack.persistence import eq
from simstack.runner_protocol import RunnerTaskStatus
from simstack.runner_runtime.watchdog.submission import submit_to_watchdog
from tests.support.in_memory_repository import InMemoryRepository


def registry(**kwargs):
    return NodeRegistry(
        name="probe", function_hash="code", arg_hash="args", func_mapping="example.probe",
        parameters=Parameters(), **kwargs,
    )


def execution(repository, entry):
    node = Node(repository=repository, func=lambda: None, is_async=False, parameters=entry.parameters)
    node.registry_entry = entry
    node._remember_registry()
    return node


@pytest.mark.asyncio
async def test_direct_terminal_write_preserves_concurrent_user_metadata_and_parent_links():
    repository = InMemoryRepository()
    entry = await repository.save(registry(status=RunnerTaskStatus.RUNNING, custom_name="before", category="before"))
    node = execution(repository, entry)
    parent = ObjectId()
    await repository.add_node_parent(entry.id, parent)
    await repository.update_node_registry(entry.id, {"custom_name": "UI name", "category": "UI category"})
    entry.message = "execution complete"
    entry.completed_at = datetime.now()
    await node.set_status(RunnerTaskStatus.COMPLETED)
    saved = await repository.find_one(NodeRegistry, eq("id", entry.id))
    assert (saved.custom_name, saved.category, saved.parent_ids) == ("UI name", "UI category", [parent])
    assert saved.status == RunnerTaskStatus.COMPLETED and saved.message == "execution complete"


@pytest.mark.asyncio
async def test_result_custom_name_cas_does_not_reject_completion_when_user_renames():
    repository = InMemoryRepository()
    entry = await repository.save(registry(status=RunnerTaskStatus.RUNNING, custom_name="before"))
    node = execution(repository, entry)
    result = SimstackResult(status=RunnerTaskStatus.COMPLETED, custom_name="result name", message="message")
    await node.process_results(result)
    await repository.update_node_registry(entry.id, {"custom_name": "UI name"})
    await node.set_status(RunnerTaskStatus.COMPLETED)
    saved = await repository.find_one(NodeRegistry, eq("id", entry.id))
    assert saved.custom_name == "UI name" and saved.message == "message"
    assert saved.status == RunnerTaskStatus.COMPLETED and saved.return_kind == "multiple"


@pytest.mark.asyncio
async def test_stale_execution_cannot_overwrite_a_new_attempt():
    repository = InMemoryRepository()
    entry = await repository.save(registry(status=RunnerTaskStatus.RUNNING, started_at=datetime(2026, 1, 1)))
    node = execution(repository, entry)
    await repository.update_node_registry(entry.id, {"status": RunnerTaskStatus.SUBMITTED, "started_at": None})
    with pytest.raises(RuntimeError, match="changed concurrently"):
        await node.set_status(RunnerTaskStatus.COMPLETED)
    assert (await repository.find_one(NodeRegistry, eq("id", entry.id))).status == RunnerTaskStatus.SUBMITTED


@pytest.mark.asyncio
async def test_parent_add_is_atomic_and_deduplicated():
    repository = InMemoryRepository()
    entry = await repository.save(registry(status=RunnerTaskStatus.COMPLETED))
    first, second = ObjectId(), ObjectId()
    await asyncio.gather(*(repository.add_node_parent(entry.id, parent) for parent in (first, second, first)))
    saved = await repository.find_one(NodeRegistry, eq("id", entry.id))
    assert set(saved.parent_ids) == {first, second} and len(saved.parent_ids) == 2


@pytest.mark.asyncio
async def test_zero_output_multiple_result_restores_metadata_and_info_documents():
    repository = InMemoryRepository()
    info = await repository.save(FileStack(name="info"))
    entry = await repository.save(registry(status=RunnerTaskStatus.COMPLETED, return_kind="multiple", custom_name="name", message="message", error="details", info_files=FileList(elements=[info])))
    node = execution(repository, await repository.find_one(NodeRegistry, eq("id", entry.id)))
    result = await node.load_results()
    assert isinstance(result, SimstackResult)
    assert (result.custom_name, result.message, result.error_message) == ("name", "message", "details")
    assert result.info_files == [info]


@pytest.mark.asyncio
async def test_direct_multiple_result_info_files_survive_partial_persistence():
    repository = InMemoryRepository()
    node = execution(repository, await repository.save(registry(status=RunnerTaskStatus.RUNNING)))
    info = FileStack(name="info")
    await node.process_results(SimstackResult(status=RunnerTaskStatus.COMPLETED, info_files=[info], message="complete"))
    await node.set_status(RunnerTaskStatus.COMPLETED)
    restored = await node.load_results()
    assert restored.info_files == [info] and restored.message == "complete"


@pytest.mark.asyncio
async def test_remote_direct_wait_keeps_waiting_for_slurm_running(monkeypatch):
    monkeypatch.setattr("simstack.models.parameters.allowed_resources.has_resource", lambda value: True)
    entry = registry(status=RunnerTaskStatus.SLURM_RUNNING)
    entry.parameters.resource = Resource(value="remote")
    repository = SimpleNamespace(find_one=AsyncMock(side_effect=[entry, entry.model_copy(update={"status": RunnerTaskStatus.COMPLETED})]))
    node = execution(repository, entry)
    node.load_results = AsyncMock(return_value="result")
    monkeypatch.setattr("simstack.core.node_persistence.context", SimpleNamespace(config=SimpleNamespace(resource=Resource(value="local"))))
    sleep = AsyncMock()
    monkeypatch.setattr("simstack.core.node_persistence.asyncio.sleep", sleep)
    assert await node.run_somewhere() == "result"
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_collector_preserves_previous_artifact_references(monkeypatch):
    repository = InMemoryRepository()
    entry = await repository.save(registry(status=RunnerTaskStatus.COMPLETED))
    old = await repository.save(ArtifactModel(name="old", parent_id=entry.id))
    entry.artifact_ids = [old.id]
    await repository.save(entry)
    executor = SimpleNamespace(status=RunnerTaskStatus.COMPLETED, name=entry.name, load_results=AsyncMock(return_value=SimstackResult(status=RunnerTaskStatus.COMPLETED)))
    monkeypatch.setattr("simstack.core.recompute_artifacts.reconstruct_persisted_node", AsyncMock(return_value=executor))
    monkeypatch.setattr("simstack.core.recompute_artifacts.import_function", AsyncMock(return_value=lambda: None))
    monkeypatch.setattr("simstack.core.recompute_artifacts.create_artifacts", AsyncMock(side_effect=RuntimeError("collector failed")))
    with pytest.raises(RuntimeError, match="collector failed"):
        await recompute_artifacts(entry, repository)
    assert (await repository.find_one(NodeRegistry, eq("id", entry.id))).artifact_ids == [old.id]
    assert await repository.find_one(ArtifactModel, eq("id", old.id)) == old


@pytest.mark.asyncio
async def test_recompute_retains_old_owned_artifact_still_referenced_by_parent(monkeypatch):
    repository = InMemoryRepository()
    entry = await repository.save(registry(status=RunnerTaskStatus.COMPLETED))
    old = await repository.save(ArtifactModel(name="shared", parent_id=entry.id))
    entry.artifact_ids = [old.id]
    await repository.save(entry)
    await repository.save(registry(status=RunnerTaskStatus.COMPLETED, artifact_ids=[old.id]))
    executor = SimpleNamespace(status=RunnerTaskStatus.COMPLETED, name=entry.name, load_results=AsyncMock(return_value=None))
    monkeypatch.setattr("simstack.core.recompute_artifacts.reconstruct_persisted_node", AsyncMock(return_value=executor))
    await recompute_artifacts(entry, repository)
    assert entry.artifact_ids == []
    assert await repository.find_one(ArtifactModel, eq("id", old.id)) == old


@pytest.mark.asyncio
async def test_recompute_collects_old_child_artifact_after_parent_references_switch(monkeypatch):
    repository = InMemoryRepository()
    parent = await repository.save(registry(status=RunnerTaskStatus.COMPLETED))
    child = await repository.save(registry(status=RunnerTaskStatus.COMPLETED, parent_ids=[parent.id]))
    old = await repository.save(ArtifactModel(name="old child", parent_id=child.id))
    for entry in (parent, child):
        entry.artifact_ids = [old.id]
        await repository.save(entry)
    executor = SimpleNamespace(status=RunnerTaskStatus.COMPLETED, name=parent.name, load_results=AsyncMock(return_value=None))
    monkeypatch.setattr("simstack.core.recompute_artifacts.reconstruct_persisted_node", AsyncMock(return_value=executor))
    await recompute_artifacts(parent, repository)
    assert (await repository.find_one(NodeRegistry, eq("id", parent.id))).artifact_ids == []
    assert (await repository.find_one(NodeRegistry, eq("id", child.id))).artifact_ids == []
    assert await repository.find_one(ArtifactModel, eq("id", old.id)) is None


@pytest.mark.asyncio
async def test_inline_code_collector_error_does_not_publish_empty_replacement(monkeypatch):
    repository = InMemoryRepository()
    entry = await repository.save(registry(status=RunnerTaskStatus.COMPLETED, call_path=".probe"))
    old = await repository.save(ArtifactModel(name="old", parent_id=entry.id))
    entry.artifact_ids = [old.id]
    await repository.save(entry)
    await repository.save(ArtifactMapping(name="broken", regex_pattern=".*", function_mapping="CODE", function_code="broken ="))
    executor = SimpleNamespace(status=RunnerTaskStatus.COMPLETED, name=entry.name, load_results=AsyncMock(return_value=SimstackResult(status=RunnerTaskStatus.COMPLETED)))
    monkeypatch.setattr("simstack.core.recompute_artifacts.reconstruct_persisted_node", AsyncMock(return_value=executor))
    monkeypatch.setattr("simstack.core.recompute_artifacts.import_function", AsyncMock(return_value=lambda: None))
    with pytest.raises(ValueError, match="Code execution failed"):
        await recompute_artifacts(entry, repository)
    assert (await repository.find_one(NodeRegistry, eq("id", entry.id))).artifact_ids == [old.id]
    assert await repository.find_one(ArtifactModel, eq("id", old.id)) == old


@pytest.mark.parametrize("job_id", [".", ".."])
def test_watchdog_rejects_dot_component_without_creating_queue(tmp_path, job_id):
    queue = tmp_path / "queue"
    with pytest.raises(ValueError, match="path component"):
        submit_to_watchdog(["true"], job_id, queue, env={"LANG": "C"})
    assert not queue.exists() and list(tmp_path.iterdir()) == []
