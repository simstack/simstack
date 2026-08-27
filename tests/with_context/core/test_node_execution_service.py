import asyncio
import os
from types import SimpleNamespace
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from simstack.core.context import context
from simstack.core.node import Node
from simstack.models.parameters import Queue, Resource, Parameters
from simstack.models import NodeRegistry
from simstack.core.definitions import TaskStatus
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.core.node_claim import claim_submitted_node

@pytest.fixture
def resource():
    return Resource(value="self")

@pytest.fixture
def node_execution_service(resource):
    return NodeExecutionService(
        resource=resource,
        interval=1,
        max_concurrent=2,
        shutdown_event=asyncio.Event(),
        detach=False
    )

@pytest.mark.asyncio
async def test_node_execution_service_execute_no_tasks(node_execution_service, initialized_context):
    with patch.object(context.db, "load_waiting_tasks_for_resource", AsyncMock(return_value=[])) as mock_load:
        await node_execution_service.execute()
        mock_load.assert_called_once_with(str(node_execution_service._resource))

@pytest.mark.asyncio
async def test_node_execution_service_execute_with_tasks(node_execution_service, initialized_context):
    registry_entry = NodeRegistry(
        name="test_node",
        input_references=[],
        status=TaskStatus.SUBMITTED,
        parameters=Parameters(),
        func_mapping="test_mapping",
        function_hash="test_func_hash",
        arg_hash="test_arg_hash"
    )
    # We don't necessarily need to save it if we mock the return of load_waiting_tasks_for_resource
    # but it's better for a "with_context" test.
    await context.db.save(registry_entry)

    with patch.object(context.db, "load_waiting_tasks_for_resource", AsyncMock(return_value=[registry_entry])), \
         patch("simstack.core.services.node_execution_service.claim_submitted_node", AsyncMock(return_value=True)), \
         patch.object(node_execution_service, "run_node", AsyncMock(return_value=True)) as mock_run:
        
        await node_execution_service.execute()
        
        # Wait for background tasks
        if node_execution_service._running_tasks:
            await asyncio.gather(*node_execution_service._running_tasks)
        
        mock_run.assert_called_once_with(registry_entry)

@pytest.mark.asyncio
async def test_node_execution_service_run_node_default_queue(node_execution_service, initialized_context):
    registry_entry = NodeRegistry(
        name="test_node",
        input_references=[],
        status=TaskStatus.SUBMITTED,
        parameters=Parameters(),
        func_mapping="test_mapping",
        function_hash="test_func_hash",
        arg_hash="test_arg_hash"
    )
    await context.db.save(registry_entry)

    with patch("simstack.core.services.node_execution_service.run_node_from_registry", AsyncMock(return_value=True)) as mock_run_reg:
        result = await node_execution_service.run_node(registry_entry)
        assert result is True
        mock_run_reg.assert_called_once_with(registry_entry)


@pytest.mark.asyncio
async def test_node_execution_service_run_node_default_docker(
    node_execution_service, initialized_context
):
    registry_entry = NodeRegistry(
        name="test_node",
        input_references=[],
        status=TaskStatus.SUBMITTED,
        parameters=Parameters(queue=Queue.DEFAULT, in_docker=True),
        func_mapping="test_mapping",
        function_hash="test_func_hash",
        arg_hash="test_arg_hash",
    )
    await context.db.save(registry_entry)

    with (
        patch(
            "simstack.core.services.node_execution_service.run_docker",
            AsyncMock(return_value=True),
        ) as mock_docker,
        patch(
            "simstack.core.services.node_execution_service.run_node_from_registry",
            AsyncMock(side_effect=AssertionError("Docker task ran as host Python")),
        ),
    ):
        result = await node_execution_service.run_node(registry_entry)

    assert result is True
    mock_docker.assert_awaited_once_with(registry_entry)


@pytest.mark.asyncio
async def test_self_docker_handoff_is_visible_to_current_resource_runner(
    initialized_context, monkeypatch
):
    registry_entry = NodeRegistry(
        name="self_docker_child",
        status=TaskStatus.SUBMITTED,
        parameters=Parameters(resource="self", in_docker=True),
        func_mapping="test_mapping",
        function_hash="self-docker-function-hash",
        arg_hash="self-docker-arg-hash",
    )
    await context.db.save(registry_entry)
    execution_node = Node.__new__(Node)
    execution_node.name = registry_entry.name
    execution_node.registry_entry = registry_entry
    execution_node.parameters = registry_entry.parameters

    monkeypatch.setattr(
        "simstack.core.node.should_dispatch_nested_docker", lambda *args: True
    )
    sentinel = SimpleNamespace(value="host-result")
    monkeypatch.setattr(
        execution_node,
        "_wait_for_remote_completion",
        AsyncMock(return_value=sentinel),
    )

    try:
        assert await execution_node.run_somewhere() is sentinel
        waiting = await context.db.load_waiting_tasks_for_resource(
            str(context.config.resource)
        )
        assert [entry.id for entry in waiting if entry.id == registry_entry.id] == [
            registry_entry.id
        ]
        saved = await context.db.load_task_by_id(registry_entry.id)
        assert str(saved.parameters.resource) == str(context.config.resource)
        assert saved.status == TaskStatus.SUBMITTED
    finally:
        await context.db.delete(registry_entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("in_docker", [False, True])
async def test_node_execution_service_run_node_slurm_queue(
    node_execution_service, initialized_context, in_docker
):
    params = Parameters(queue=Queue.SLURM_QUEUE, in_docker=in_docker)
    registry_entry = NodeRegistry(
        name="test_node",
        input_references=[],
        status=TaskStatus.SUBMITTED,
        parameters=params,
        func_mapping="test_mapping",
        function_hash="test_func_hash",
        arg_hash="test_arg_hash"
    )
    await context.db.save(registry_entry)

    with patch("simstack.core.services.node_execution_service.submit_node", AsyncMock(return_value=True)) as mock_submit:
        result = await node_execution_service.run_node(registry_entry)
        assert result is True
        mock_submit.assert_called_once_with(registry_entry)
        assert registry_entry.parameters.in_docker is in_docker


@pytest.mark.asyncio
async def test_detached_spawn_failure_is_sanitized_and_marks_task_failed(
    resource, initialized_context, monkeypatch, caplog
):
    secret = "mongodb://runner-user:runner-password@db.internal:27017/simstack"
    fake_context = SimpleNamespace(
        config=SimpleNamespace(
            connection_string=secret,
            db_name="runner-database",
            project_root=context.config.project_root,
            python_paths=[context.config.project_root / "src"],
            resource=context.config.resource,
        ),
        db=context.db,
    )
    monkeypatch.setattr(
        "simstack.core.services.node_execution_service.context", fake_context
    )
    service = NodeExecutionService(
        resource=resource,
        interval=1,
        max_concurrent=1,
        shutdown_event=None,
        detach=True,
    )
    registry_entry = NodeRegistry(
        name="test_node",
        status=TaskStatus.SUBMITTED,
        parameters=Parameters(queue=Queue.DEFAULT, in_docker=False),
        func_mapping="test_mapping",
        function_hash="test_func_hash",
        arg_hash="test_arg_hash",
    )
    await context.db.save(registry_entry)

    monkeypatch.setattr(service, "write_node_event", AsyncMock())
    spawn = AsyncMock(side_effect=OSError(f"spawn failed at {secret}"))
    monkeypatch.setattr(
        "simstack.core.services.node_execution_service.asyncio.create_subprocess_exec",
        spawn,
    )

    assert await service.run_node(registry_entry) is False
    assert registry_entry.status == TaskStatus.FAILED
    assert secret not in (registry_entry.error or "")
    assert "runner-user" not in caplog.text
    assert "runner-password" not in caplog.text
    child_env = spawn.await_args.kwargs["env"]
    assert child_env["SIMSTACK_DB_CONNECTION_STRING"] == secret
    assert child_env["SIMSTACK_DB_DATABASE"] == "runner-database"
    assert child_env["PYTHONPATH"].split(os.pathsep)[:2] == [
        str(context.config.project_root),
        str(context.config.project_root / "src"),
    ]


@pytest.mark.asyncio
async def test_direct_system_exit_marks_task_failed(
    node_execution_service, initialized_context
):
    registry_entry = NodeRegistry(
        name="system_exit_node",
        status=TaskStatus.SUBMITTED,
        parameters=Parameters(queue=Queue.DEFAULT, in_docker=False),
        func_mapping="test_mapping",
        function_hash="test_func_hash",
        arg_hash="test_arg_hash",
    )
    await context.db.save(registry_entry)

    with patch(
        "simstack.core.services.node_execution_service.run_node_from_registry",
        AsyncMock(side_effect=SystemExit(0)),
    ):
        result = await node_execution_service.run_node(registry_entry)

    assert result is False
    assert registry_entry.status == TaskStatus.FAILED
    assert registry_entry.return_kind == "exception"
    assert registry_entry.error == "0"


@pytest.mark.asyncio
async def test_nested_docker_handoff_does_not_requeue_claimed_task(
    initialized_context, monkeypatch
):
    registry_entry = NodeRegistry(
        name="claimed_docker_child",
        status=TaskStatus.SUBMITTED,
        parameters=Parameters(resource="self", in_docker=False),
        func_mapping="test_mapping",
        function_hash="claimed-docker-function-hash",
        arg_hash="claimed-docker-arg-hash",
    )
    await context.db.save(registry_entry)
    stale_submitted = registry_entry.model_copy(deep=True)
    assert await claim_submitted_node(registry_entry) is True

    execution_node = Node.__new__(Node)
    execution_node.name = registry_entry.name
    execution_node.registry_entry = stale_submitted
    execution_node.parameters = stale_submitted.parameters

    assert await execution_node._persist_nested_docker_wait() is False
    current = await context.db.load_task_by_id(registry_entry.id)
    assert current.status == TaskStatus.RETRIEVED
    assert current.parameters.in_docker is False
