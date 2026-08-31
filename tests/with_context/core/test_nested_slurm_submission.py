import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import Node, _find_reusable_task, node
from simstack.core.node_claim import (
    claim_submitted_node,
)
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.models import (
    FloatData,
    NamedDataReference,
    NodeModel,
    NodeRegistry,
    ResourceAssignmentRule,
    SlurmParametersPatch,
)
from simstack.models.parameters import Parameters, Resource, SlurmParameters


@node
def sync_nested_slurm_failure_node(**kwargs) -> FloatData:
    return FloatData(value=1.0)


def nested_slurm_route_probe(**kwargs):
    return None


async def _register_nested_slurm_route_probe():
    name = nested_slurm_route_probe.__name__
    mapping = await context.db.find_one(NodeModel, NodeModel.name == name)
    if mapping is None:
        mapping = await context.db.save(
            NodeModel(
                name=name,
                function_mapping=f"{__name__}.{name}",
                input_mappings=[],
                result_mappings=[],
                default_parameters=Parameters(),
            )
        )
    context.node_mappings._by_name[name] = mapping
    context.node_mappings._by_mapping[mapping.function_mapping] = mapping


async def _delete_nested_slurm_route_probe_data():
    entries = await context.db.find(
        NodeRegistry,
        NodeRegistry.name == nested_slurm_route_probe.__name__,
    )
    for entry in entries:
        await context.db.delete(entry)

    rules = await context.db.find(
        ResourceAssignmentRule,
        ResourceAssignmentRule.name == "nested-slurm-route-probe",
    )
    for rule in rules:
        await context.db.delete(rule)


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


def _result_reference() -> NamedDataReference:
    return NamedDataReference(
        variable_name="result",
        variable_mapping="simstack.models.FloatData",
        reference=FloatData(value=1.0).id,
    )


@pytest.mark.asyncio
async def test_completed_result_is_reusable_after_execution_route_change():
    completed = _slurm_registry("completed_route_cache", TaskStatus.COMPLETED)
    completed.parameters = Parameters(resource="self", queue="default")
    db = SimpleNamespace(find=AsyncMock(return_value=[completed]))

    reusable = await _find_reusable_task(
        db,
        name=completed.name,
        arg_hash=completed.arg_hash,
        function_hash=completed.function_hash,
        execution_parameters=Parameters(
            resource="test",
            queue="slurm-queue",
            slurm_parameters=SlurmParameters(nodes=2, mem="4G"),
        ),
    )

    assert reusable is completed


@pytest.mark.asyncio
async def test_active_task_is_reusable_only_on_the_same_execution_route():
    active = _slurm_registry("active_route_cache", TaskStatus.RETRIEVED)
    db = SimpleNamespace(find=AsyncMock(return_value=[active]))

    same_route = await _find_reusable_task(
        db,
        name=active.name,
        arg_hash=active.arg_hash,
        function_hash=active.function_hash,
        execution_parameters=active.parameters.model_copy(deep=True),
    )
    changed_route = await _find_reusable_task(
        db,
        name=active.name,
        arg_hash=active.arg_hash,
        function_hash=active.function_hash,
        execution_parameters=Parameters(resource="self", queue="default"),
    )

    assert same_route is active
    assert changed_route is None


@pytest.mark.asyncio
async def test_changed_nested_route_ignores_stale_self_cache_and_submits_slurm(
    monkeypatch,
):
    await _delete_nested_slurm_route_probe_data()
    await _register_nested_slurm_route_probe()
    parent_parameters = Parameters(resource="test", queue="default")
    call_path = ".workflow.nested_slurm_route_probe"

    try:
        stale_node = Node(
            func=nested_slurm_route_probe,
            is_async=False,
            parameters=Parameters(),
            parent_parameters=parent_parameters,
            call_path=call_path,
        )
        assert await stale_node.get_node_registry() == TaskStatus.RETRIEVED
        assert stale_node.registry_entry is not None
        stale_id = stale_node.registry_entry.id
        assert stale_node.parameters.resource == "self"
        assert stale_node.parameters.queue == "default"

        await context.db.save(
            ResourceAssignmentRule(
                name="nested-slurm-route-probe",
                regex_pattern="workflow.nested_slurm_route_probe",
                resource_str="test",
                queue="slurm-queue",
                slurm_parameters_patch=SlurmParametersPatch(nodes=2, mem="4G"),
            )
        )

        slurm_node = Node(
            func=nested_slurm_route_probe,
            is_async=False,
            parameters=Parameters(),
            parent_parameters=parent_parameters,
            call_path=call_path,
        )
        assert await slurm_node.get_node_registry() == TaskStatus.SUBMITTED
        assert slurm_node.registry_entry is not None
        assert slurm_node.registry_entry.id != stale_id
        assert slurm_node.parameters.resource == "test"
        assert slurm_node.parameters.queue == "slurm-queue"
        assert slurm_node.parameters.slurm_parameters.nodes == 2
        assert slurm_node.parameters.slurm_parameters.mem == "4G"
        assert slurm_node.registry_entry.assignment_rule_name == (
            "nested-slurm-route-probe"
        )

        persisted_stale = await context.db.load_task_by_id(stale_id)
        assert persisted_stale.status == TaskStatus.RETRIEVED
        assert persisted_stale.parameters.resource == "self"
        assert persisted_stale.parameters.queue == "default"

        submitted_ids = []
        sentinel = SimpleNamespace(value="nested-slurm-result")

        async def fake_submit_node(entry):
            submitted_ids.append(entry.id)
            assert entry.status == TaskStatus.RETRIEVED
            entry.status = TaskStatus.SLURM_QUEUED
            await context.db.save(entry)
            return True

        async def fake_wait_for_remote_completion(self):
            return sentinel

        async def fail_if_executed_locally(self):
            raise AssertionError("the changed child route must be submitted to Slurm")

        monkeypatch.setattr("simstack.core.submit_node.submit_node", fake_submit_node)
        monkeypatch.setattr(
            Node, "_wait_for_remote_completion", fake_wait_for_remote_completion
        )
        monkeypatch.setattr(Node, "run_node_as_process", fail_if_executed_locally)

        assert await slurm_node.run_somewhere() is sentinel
        assert submitted_ids == [slurm_node.registry_entry.id]
    finally:
        await _delete_nested_slurm_route_probe_data()


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
        entry.results_references = [_result_reference()]
        await context.db.save(entry)

    async def fake_load_results(self):
        return sentinel

    monkeypatch.setattr("simstack.core.submit_node.submit_node", fake_submit_node)
    monkeypatch.setattr(Node, "load_results", fake_load_results)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert submitted_ids == [registry_entry.id]


@pytest.mark.asyncio
async def test_nested_slurm_submit_failure_stops_polling(monkeypatch):
    registry_entry = await context.db.save(_slurm_registry("failed_inline_slurm_child"))
    execution_node = _execution_node(registry_entry)

    async def fail_submit(entry):
        entry.status = TaskStatus.FAILED
        await context.db.save(entry)
        return False

    monkeypatch.setattr("simstack.core.submit_node.submit_node", fail_submit)

    with pytest.raises(RuntimeError, match="terminated with status"):
        await execution_node.run_somewhere()

    saved_entry = await context.db.load_task_by_id(registry_entry.id)
    assert saved_entry.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_sync_node_wrapper_raises_when_nested_slurm_submit_fails(monkeypatch):
    async def fail_submit(entry):
        entry.status = TaskStatus.FAILED
        await context.db.save(entry)
        return False

    monkeypatch.setattr("simstack.core.submit_node.submit_node", fail_submit)

    with pytest.raises(RuntimeError, match="terminated with status"):
        sync_nested_slurm_failure_node(
            parameters=Parameters(
                resource="test",
                queue="slurm-queue",
                slurm_parameters=SlurmParameters(nodes=1),
                force_rerun=True,
            )
        )

    entries = await context.db.find(
        NodeRegistry,
        NodeRegistry.name == "sync_nested_slurm_failure_node",
    )
    assert entries
    assert entries[-1].status == TaskStatus.FAILED
    for entry in entries:
        await context.db.delete(entry)


@pytest.mark.asyncio
async def test_already_claimed_slurm_child_is_not_submitted_twice(monkeypatch):
    registry_entry = await context.db.save(
        _slurm_registry("already_claimed_slurm_child", status=TaskStatus.RETRIEVED)
    )
    execution_node = _execution_node(registry_entry)
    submitted_ids = []

    async def fake_submit(entry):
        submitted_ids.append(entry.id)
        return True

    monkeypatch.setattr("simstack.core.submit_node.submit_node", fake_submit)

    assert await execution_node._submit_same_resource_slurm_node() is False
    assert submitted_ids == []


@pytest.mark.asyncio
async def test_submitted_slurm_node_is_claimed_once_before_sbatch(monkeypatch):
    registry_entry = await context.db.save(_slurm_registry("single_sbatch_child"))
    first = _execution_node(registry_entry.model_copy(deep=True))
    second = _execution_node(registry_entry.model_copy(deep=True))
    submitted_ids = []

    async def fake_submit(entry):
        submitted_ids.append(entry.id)
        return True

    monkeypatch.setattr("simstack.core.submit_node.submit_node", fake_submit)

    first_submitted, second_submitted = await asyncio.gather(
        first._submit_same_resource_slurm_node(),
        second._submit_same_resource_slurm_node(),
    )

    assert sorted([first_submitted, second_submitted]) == [False, True]
    assert submitted_ids == [registry_entry.id]


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
