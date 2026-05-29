from types import SimpleNamespace

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import Node
from simstack.core.node_claim import claim_submitted_node
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.models import NodeRegistry
from simstack.models.parameters import Parameters, Resource, SlurmParameters


def _slurm_registry(name: str, status: TaskStatus = TaskStatus.SUBMITTED):
    return NodeRegistry(
        name=name,
        status=status,
        function_hash=f"{name}-function-hash",
        arg_hash=f"{name}-arg-hash",
        func_mapping=f"tests:{name}",
        parameters=Parameters(
            resource="test",
            queue="slurm-queue",
            slurm_parameters=SlurmParameters(nodes=1),
        ),
    )


def _execution_node(registry_entry: NodeRegistry) -> Node:
    execution_node = Node.__new__(Node)
    execution_node.name = registry_entry.name
    execution_node.registry_entry = registry_entry
    execution_node.parameters = registry_entry.parameters
    return execution_node


@pytest.mark.asyncio
async def test_claim_submitted_node_only_claims_once():
    registry_entry = await context.db.save(_slurm_registry("claim_once_child"))

    assert await claim_submitted_node(registry_entry) is True
    assert registry_entry.status == TaskStatus.RETRIEVED
    assert await claim_submitted_node(registry_entry) is False

    saved_entry = await context.db.load_task_by_id(registry_entry.id)
    assert saved_entry.status == TaskStatus.RETRIEVED


@pytest.mark.asyncio
async def test_nested_slurm_child_is_submitted_inline_on_current_resource(monkeypatch):
    registry_entry = await context.db.save(_slurm_registry("inline_slurm_child"))
    execution_node = _execution_node(registry_entry)
    submitted_ids = []
    sentinel = SimpleNamespace(value="nested-result")

    async def fake_submit_node(entry):
        submitted_ids.append(entry.id)
        entry.status = TaskStatus.COMPLETED
        await context.db.save(entry)

    async def fake_load_results(self):
        return sentinel

    monkeypatch.setattr("simstack.core.submit_node.submit_node", fake_submit_node)
    monkeypatch.setattr(Node, "load_results", fake_load_results)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert submitted_ids == [registry_entry.id]


@pytest.mark.skip(reason="fix-alter")
@pytest.mark.asyncio
async def test_nested_slurm_child_waits_when_already_claimed(monkeypatch):
    registry_entry = await context.db.save(
        _slurm_registry("already_claimed_slurm_child", status=TaskStatus.RETRIEVED)
    )
    execution_node = _execution_node(registry_entry)
    sentinel = SimpleNamespace(value="claimed-result")

    async def fail_if_submitted(entry):
        raise AssertionError("already claimed nodes must not be submitted twice")

    async def fake_load_task_by_id(task_id):
        registry_entry.status = TaskStatus.COMPLETED
        return registry_entry

    async def fake_load_results(self):
        return sentinel

    monkeypatch.setattr("simstack.core.submit_node.submit_node", fail_if_submitted)
    monkeypatch.setattr(context.db, "load_task_by_id", fake_load_task_by_id)
    monkeypatch.setattr(Node, "load_results", fake_load_results)

    result = await execution_node.run_somewhere()

    assert result is sentinel


@pytest.mark.asyncio
async def test_runner_skips_stale_entry_when_another_process_claimed_it(monkeypatch):
    registry_entry = await context.db.save(
        _slurm_registry("runner_stale_slurm_child", status=TaskStatus.RETRIEVED)
    )
    service = NodeExecutionService(
        Resource(value="test"),
        interval=1,
        max_concurrent=1,
        shutdown_event=None,
        detach=False,
    )

    async def fake_write_resource_event(*args, **kwargs):
        return None

    async def fake_load_waiting_tasks_for_resource(resource):
        return [registry_entry]

    async def fail_if_run(entry):
        raise AssertionError("stale entries must not be executed after claim failed")

    monkeypatch.setattr(service, "write_resource_event", fake_write_resource_event)
    monkeypatch.setattr(
        context.db,
        "load_waiting_tasks_for_resource",
        fake_load_waiting_tasks_for_resource,
    )
    monkeypatch.setattr(service, "_run_with_semaphore", fail_if_run)

    await service.execute()

    assert service._running_tasks == set()
