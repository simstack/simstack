import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from simstack.core.context import context
from simstack.models.parameters import Resource, Parameters
from simstack.models import NodeRegistry
from simstack.core.definitions import TaskStatus
from simstack.core.services.node_execution_service import NodeExecutionService

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
async def test_node_execution_service_run_node_slurm_queue(node_execution_service, initialized_context):
    from simstack.models.parameters import Queue
    params = Parameters()
    params.queue = Queue.SLURM_QUEUE
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
