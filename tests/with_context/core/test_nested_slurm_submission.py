"""Nested routing/cache contracts through Server-owned publication and claims."""
from simstack.runner_runtime.launch_claims import acquire_work
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import Node, compute_arg_hash
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.models import NodeModel, NodeRegistry, ResourceAssignmentRule, SlurmParametersPatch
from simstack.models.parameters import Parameters, Resource, SlurmParameters
from simstack.runner_runtime.launch_claims import task_execution


def nested_slurm_route_probe(**kwargs):
    return True


@pytest_asyncio.fixture(autouse=True)
async def register_probe(server_storage):
    await server_storage.save(NodeModel(name="nested_slurm_route_probe",
        function_mapping=f"{__name__}.nested_slurm_route_probe", input_mappings=[],
        result_mappings=[], default_parameters=Parameters()))
    await context.refresh_mappings()


async def running_parent(server_storage):
    parent = NodeRegistry(name="parent", status=TaskStatus.SUBMITTED,
        function_hash="", arg_hash="parent", func_mapping="test.parent",
        parameters=Parameters(resource="test"))
    await server_storage.save(parent)
    entry = (await acquire_work(context.server, capacity=1))[0]
    with task_execution(entry):
        await context.server.tasks.start_attempt(entry)
    return await context.server.tasks.get_task(entry.id)


async def child_for(parent, parameters):
    value = Node(func=nested_slurm_route_probe, is_async=False, parent_id=parent.id,
        parameters=parameters, call_path=".workflow.nested_slurm_route_probe")
    with task_execution(parent):
        await value.get_node_registry()
    return value


def close_reservation(node):
    handle = getattr(node, "_inline_launch_reservation", None)
    if handle is not None:
        handle.close()


@pytest.mark.asyncio
async def test_completed_result_is_reusable_after_execution_route_change(server_storage):
    parent = await running_parent(server_storage)
    completed = NodeRegistry(name="nested_slurm_route_probe", status=TaskStatus.COMPLETED,
        function_hash="old-ignored-function-hash", arg_hash=compute_arg_hash([]),
        func_mapping=f"{__name__}.nested_slurm_route_probe", parameters=Parameters(resource="test"))
    await server_storage.save(completed)
    node = await child_for(parent, Parameters(resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters(tasks=1, tasks_per_node=None)))
    assert node.id == completed.id
    assert node.status == TaskStatus.COMPLETED
    assert not node._owns_inline_nested_execution


@pytest.mark.asyncio
async def test_active_task_is_reusable_only_on_the_same_execution_route(server_storage):
    parent = await running_parent(server_storage)
    first = await child_for(parent, Parameters(resource="self"))
    try:
        same = await child_for(parent, Parameters(resource="self"))
        changed = await child_for(parent, Parameters(resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters(tasks=1, tasks_per_node=None)))
        assert same.id == first.id
        assert not same._owns_inline_nested_execution
        assert changed.id != first.id
        assert changed.status == TaskStatus.SUBMITTED
    finally:
        close_reservation(first)


@pytest.mark.asyncio
async def test_changed_nested_route_ignores_stale_self_cache_and_waits_for_runner(server_storage, monkeypatch):
    parent = await running_parent(server_storage)
    first = await child_for(parent, Parameters(resource="self"))
    try:
        await server_storage.save(ResourceAssignmentRule(name="nested-slurm-route-probe",
            regex_pattern="workflow.nested_slurm_route_probe", resource_str="test",
            queue="slurm-queue", slurm_parameters_patch=SlurmParametersPatch(nodes=2, mem="4G")))
        routed = await child_for(parent, Parameters(resource="self"))
        assert routed.id != first.id
        assert routed.status == TaskStatus.SUBMITTED
        assert routed.parameters.queue == "slurm-queue"
        assert routed.parameters.slurm_parameters.nodes == 2
        assert routed.parameters.slurm_parameters.mem == "4G"
        assert routed.registry_entry.assignment_rule_name == "nested-slurm-route-probe"
        sentinel = object()
        monkeypatch.setattr(routed, "_wait_for_remote_completion", AsyncMock(return_value=sentinel))
        monkeypatch.setattr(routed, "run_node_as_process", AsyncMock(side_effect=AssertionError("Slurm ran inline")))
        assert await routed.run_somewhere() is sentinel
        assert (await context.server.tasks.get_task(first.id)).status == TaskStatus.RETRIEVED
    finally:
        close_reservation(first)


@pytest.mark.asyncio
async def test_acquire_submitted_node_only_claims_once(server_storage):
    parent = await running_parent(server_storage)
    child = await child_for(parent, Parameters(resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters(tasks=1, tasks_per_node=None)))
    first = await acquire_work(context.server, capacity=1)
    assert [entry.id for entry in first] == [child.id]
    assert await acquire_work(context.server, capacity=1) == []
    assert (await context.server.tasks.get_task(child.id)).status == TaskStatus.RETRIEVED


@pytest.mark.asyncio
async def test_nested_slurm_child_stays_submitted_for_host_runner(server_storage, monkeypatch):
    parent = await running_parent(server_storage)
    child = await child_for(parent, Parameters(resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters(tasks=1, tasks_per_node=None)))
    sentinel = object()
    monkeypatch.setattr(child, "_wait_for_remote_completion", AsyncMock(return_value=sentinel))
    assert await child.run_somewhere() is sentinel
    assert (await context.server.tasks.get_task(child.id)).status == TaskStatus.SUBMITTED


@pytest.mark.asyncio
async def test_nested_slurm_wait_raises_when_host_runner_fails(server_storage):
    parent = await running_parent(server_storage)
    child = await child_for(parent, Parameters(resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters(tasks=1, tasks_per_node=None)))
    entry = (await acquire_work(context.server, capacity=1))[0]
    with task_execution(entry):
        await context.server.tasks.fail_attempt(entry, "host bootstrap failed", phase="bootstrap")
    with pytest.raises(RuntimeError, match="host bootstrap failed"):
        await child._wait_for_remote_completion()
    assert (await context.server.tasks.get_task(child.id)).error == "host bootstrap failed"


@pytest.mark.asyncio
async def test_runner_skips_already_claimed_task_on_next_poll(server_storage, monkeypatch):
    parent = await running_parent(server_storage)
    child = await child_for(parent, Parameters(resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters(tasks=1, tasks_per_node=None)))
    await acquire_work(context.server, capacity=1)
    # No local intent remains for this independently held claim.
    from simstack.runner_runtime.launch_claims import receipt, finish
    finish(context.server, receipt(context.server, child.id), claimed=False)
    service = NodeExecutionService(Resource(value="test"), 1, 1, None, detach=False)
    monkeypatch.setattr(service, "run_node", AsyncMock(side_effect=AssertionError("duplicate execution")))
    await service.execute()
    assert not service._running_tasks
    assert (await context.server.tasks.get_task(child.id)).status == TaskStatus.RETRIEVED


@pytest.mark.asyncio
async def test_default_slurm_parameters_form_a_valid_submission(server_storage):
    parent = await running_parent(server_storage)
    parameters = Parameters(resource="test", queue="slurm-queue")
    assert parameters.slurm_parameters.tasks == 1
    assert parameters.slurm_parameters.tasks_per_node == 1
    child = await child_for(parent, parameters)
    assert child.status == TaskStatus.SUBMITTED
    assert child.parameters.slurm_parameters.nodes == 1
    assert child.parameters.slurm_parameters.tasks is None
    assert child.parameters.slurm_parameters.tasks_per_node is None


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_total_tasks_and_per_node_limit_are_both_preserved(server_storage, count):
    parent = await running_parent(server_storage)
    child = await child_for(parent, Parameters(resource="test", queue="slurm-queue",
        slurm_parameters=SlurmParameters(nodes=1, tasks=count, tasks_per_node=count)))
    assert child.status == TaskStatus.SUBMITTED
    assert child.parameters.slurm_parameters.tasks == count
    assert child.parameters.slurm_parameters.tasks_per_node == count
