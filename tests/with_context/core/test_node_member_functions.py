import pytest
import os
import asyncio
from pathlib import Path
from odmantic import Model, ObjectId
from simstack.core.context import context
from simstack.core.node import Node, node
from simstack.models import FloatData, Parameters, NodeRegistry, ModelMapping, NodeModel
from simstack.core.definitions import TaskStatus
from simstack.core.simstack_result import SimstackResult
from simstack.models.parameters import Resource, Queue
from simstack.core.resources import allowed_resources

# Define some test nodes
@node
def sync_node(data: FloatData, **kwargs) -> FloatData:
    return FloatData(value=data.value + 1.0)

@node
async def async_node(data: FloatData, **kwargs) -> FloatData:
    await asyncio.sleep(0.01)
    return FloatData(value=data.value + 2.0)

@node
def failing_node(data: FloatData, **kwargs) -> FloatData:
    raise RuntimeError("Intentional failure")

@pytest.mark.asyncio
async def test_node_init(initialized_context):
    """Test Node initialization with various args and kwargs."""
    params = Parameters()
    data = FloatData(value=1.0)
    
    # Test basic init
    n = Node(data, func=sync_node._inner, is_async=False, parameters=params)
    assert n.name == "sync_node"
    assert n.is_async is False
    assert len(n._args) == 1
    assert n._args[0] == data
    assert n.parameters == params
    assert n.registry_entry is None
    
    # Test init with kwargs matching function parameters
    n2 = Node(func=sync_node._inner, is_async=False, parameters=params, data=data)
    assert len(n2._args) == 1
    assert n2._args[0] == data
    
    # Test custom_name
    n3 = Node(data, func=sync_node._inner, is_async=False, parameters=params, custom_name="my_custom_node")
    assert n3.custom_name == "my_custom_node"

@pytest.mark.asyncio
async def test_node_properties(initialized_context):
    """Test id and status properties."""
    params = Parameters()
    data = FloatData(value=1.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=params)
    
    # Before registry entry
    assert n.id is None
    assert n.status == TaskStatus.FAILED # Default when registry_entry is None
    
    # Mock registry entry
    entry = NodeRegistry(
        name="sync_node",
        status=TaskStatus.SUBMITTED,
        function_hash="h1",
        arg_hash="h2",
        func_mapping="mapping",
        parameters=params
    )
    n.registry_entry = entry
    assert n.id == entry.id
    assert n.status == TaskStatus.SUBMITTED

@pytest.mark.asyncio
async def test_make_registry_entry_success(initialized_context):
    """Test successful creation of registry entry."""
    # Ensure mappings exist
    node_model = NodeModel(
        name="sync_node",
        function_mapping="simstack_tests.with_context.core.test_node_member_functions.sync_node",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    
    model_mapping = ModelMapping(
        name="FloatData",
        mapping="simstack.models.FloatData",
        collection_name="float_data"
    )
    await context.db.save(model_mapping)
    await context.refresh_mappings()
    
    data = FloatData(value=1.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
    
    entry = await n.make_registry_entry("func_hash", "arg_hash")
    assert entry is not None
    assert entry.name == "sync_node"
    assert entry.function_hash == "func_hash"
    assert entry.arg_hash == "arg_hash"
    assert n.registry_entry == entry
    assert len(entry.input_ids) == 1

@pytest.mark.asyncio
async def test_make_registry_entry_failure(initialized_context):
    """Test failure conditions for make_registry_entry."""
    from unittest.mock import patch, MagicMock
    
    # Missing node mapping
    data = FloatData(value=1.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
    
    # We need to mock BOTH context.node_mappings.get_by_name AND db.find_one to return None
    # to trigger "Could not find function mapping"
    with patch.object(context.node_mappings, "get_by_name", return_value=None):
        with patch.object(context.db, "find_one", return_value=None):
            with pytest.raises(ValueError, match="Could not find function mapping"):
                await n.make_registry_entry("h1", "h2")
        
    # Missing model mapping
    # 1. Setup a valid node mapping (real or mocked)
    node_model = NodeModel(
        name="sync_node",
        function_mapping="simstack_tests.with_context.core.test_node_member_functions.sync_node",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    await context.refresh_mappings()
    
    # 2. Mock context.model_mappings.get_by_name AND ensure db.find_one doesn't accidentally return None for the node mapping
    # Actually, if we use a real NodeModel in DB, we just need to mock model_mappings
    with patch.object(context.model_mappings, "get_by_name", return_value=None):
        with patch.object(context.db, "find_one", side_effect=[node_model, None]):
             with pytest.raises(ValueError, match="Could not find table name"):
                await n.make_registry_entry("h1", "h2")

@pytest.mark.asyncio
async def test_get_node_registry(initialized_context):
    """Test get_node_registry including caching and force_rerun."""
    # Setup mappings
    node_model = NodeModel(
        name="sync_node",
        function_mapping="simstack_tests.with_context.core.test_node_member_functions.sync_node",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    model_mapping = ModelMapping(
        name="FloatData",
        mapping="simstack.models.FloatData",
        collection_name="float_data"
    )
    await context.db.save(model_mapping)
    await context.refresh_mappings()
    
    data = FloatData(value=1.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
    
    # 1. First call - should create new entry
    status = await n.get_node_registry()
    assert status == TaskStatus.SUBMITTED
    first_id = n.id
    
    # 2. Second call - should load from DB
    n2 = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
    status2 = await n2.get_node_registry()
    assert status2 == TaskStatus.SUBMITTED
    assert n2.id == first_id
    
    # 3. Call with force_rerun - should create new entry
    params_rerun = Parameters(force_rerun=True)
    n3 = Node(data, func=sync_node._inner, is_async=False, parameters=params_rerun)
    status3 = await n3.get_node_registry()
    assert status3 == TaskStatus.SUBMITTED
    assert n3.id != first_id

@pytest.mark.asyncio
async def test_load_results(initialized_context):
    """Test load_results for various cases."""
    params = Parameters()
    data = FloatData(value=1.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=params)
    
    # 1. Status not completed
    entry = NodeRegistry(name="sync_node", status=TaskStatus.RUNNING, function_hash="h1", arg_hash="h2", func_mapping="m1", parameters=params)
    n.registry_entry = entry
    assert await n.load_results() is None
    
    # 2. Completed but no result identifiers
    entry.status = TaskStatus.COMPLETED
    # Should log warning but not raise if no tables/ids
    res = await n.load_results()
    assert isinstance(res, SimstackResult)
    
    # 3. Inconsistent results
    entry.result_tables = ["Table1"]
    entry.result_ids = []
    with pytest.raises(ValueError, match="has inconsistent results"):
        await n.load_results()
        
    # 4. Successful load
    res_data = FloatData(value=5.0)
    await context.db.save(res_data)
    
    # Re-setup mappings and refresh
    model_mapping = ModelMapping(name="FloatData", mapping="simstack.models.FloatData", collection_name="float_data")
    await context.db.save(model_mapping)
    await context.refresh_mappings()
    
    entry.status = TaskStatus.COMPLETED
    entry.result_tables = ["simstack.models.FloatData"]
    entry.result_ids = [res_data.id]
    entry.result_names = ["output"]
    await context.db.save(entry) # Ensure it's saved if load_results re-loads
    
    loaded_res = await n.load_results()
    assert loaded_res is not None
    assert isinstance(loaded_res, FloatData)
    assert loaded_res.value == 5.0

@pytest.mark.asyncio
async def test_execute_node_locally_sync(initialized_context):
    """Test local execution of a synchronous node."""
    # Setup mappings
    node_model = NodeModel(name="sync_node", function_mapping="simstack_tests.with_context.core.test_node_member_functions.sync_node", input_mappings=[], default_parameters=Parameters())
    await context.db.save(node_model)
    model_mapping = ModelMapping(name="FloatData", mapping="simstack.models.FloatData", collection_name="float_data")
    await context.db.save(model_mapping)
    await context.refresh_mappings()
    
    data = FloatData(value=10.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
    await n.get_node_registry()
    
    result = await n.execute_node_locally()
    assert isinstance(result, FloatData)
    assert result.value == 11.0
    assert n.status == TaskStatus.COMPLETED

@pytest.mark.asyncio
async def test_execute_node_locally_async(initialized_context):
    """Test local execution of an asynchronous node."""
    # Setup mappings
    node_model = NodeModel(name="async_node", function_mapping="simstack_tests.with_context.core.test_node_member_functions.async_node", input_mappings=[], default_parameters=Parameters())
    await context.db.save(node_model)
    model_mapping = ModelMapping(name="FloatData", mapping="simstack.models.FloatData", collection_name="float_data")
    await context.db.save(model_mapping)
    await context.refresh_mappings()
    
    data = FloatData(value=10.0)
    n = Node(data, func=async_node._inner, is_async=True, parameters=Parameters())
    await n.get_node_registry()
    
    result = await n.execute_node_locally()
    assert isinstance(result, FloatData)
    assert result.value == 12.0
    assert n.status == TaskStatus.COMPLETED

@pytest.mark.asyncio
async def test_execute_node_locally_failure(initialized_context):
    """Test failure during local execution."""
    # Setup mappings
    node_model = NodeModel(name="failing_node", function_mapping="simstack_tests.with_context.core.test_node_member_functions.failing_node", input_mappings=[], default_parameters=Parameters())
    await context.db.save(node_model)
    model_mapping = ModelMapping(name="FloatData", mapping="simstack.models.FloatData", collection_name="float_data")
    await context.db.save(model_mapping)
    await context.refresh_mappings()
    
    data = FloatData(value=10.0)
    n = Node(data, func=failing_node._inner, is_async=False, parameters=Parameters())
    await n.get_node_registry()
    
    with pytest.raises(RuntimeError, match="Intentional failure"):
        await n.execute_node_locally()
    
    assert n.status == TaskStatus.FAILED

@pytest.mark.asyncio
async def test_set_status(initialized_context):
    """Test status updates."""
    params = Parameters()
    data = FloatData(value=1.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=params)
    
    with pytest.raises(ValueError, match="Task has no registry entry"):
        await n.set_status(TaskStatus.RUNNING)
    
    entry = NodeRegistry(name="sync_node", status=TaskStatus.SUBMITTED, function_hash="h1", arg_hash="h2", func_mapping="m1", parameters=params)
    await context.db.save(entry)
    n.registry_entry = entry
    
    await n.set_status(TaskStatus.RUNNING)
    assert n.status == TaskStatus.RUNNING
    
    # Check DB
    updated_entry = await context.db.find_one(NodeRegistry, NodeRegistry.id == entry.id)
    assert updated_entry.status == TaskStatus.RUNNING

@pytest.mark.asyncio
async def test_run_somewhere_local(initialized_context):
    """Test run_somewhere routing to local execution."""
    # Setup mappings
    node_model = NodeModel(name="sync_node", function_mapping="simstack_tests.with_context.core.test_node_member_functions.sync_node", input_mappings=[], default_parameters=Parameters())
    await context.db.save(node_model)
    model_mapping = ModelMapping(name="FloatData", mapping="simstack.models.FloatData", collection_name="float_data")
    await context.db.save(model_mapping)
    await context.refresh_mappings()

    # Add 'local' to allowed resources
    original_resources = allowed_resources.get_resources()
    allowed_resources._resources.append("local")
    
    try:
        data = FloatData(value=10.0)
        n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters(resource="local"))
        await n.get_node_registry()

        # context.config.resource is "local" in tests by default usually
        result = await n.run_somewhere()
        assert result.value == 11.0
    finally:
        allowed_resources._resources = original_resources

@pytest.mark.asyncio
async def test_process_results_variants(initialized_context):
    """Test process_results with different return types."""
    params = Parameters()
    n = Node(func=sync_node._inner, is_async=False, parameters=params)
    entry = NodeRegistry(name="sync_node", status=TaskStatus.RUNNING, function_hash="h1", arg_hash="h2", func_mapping="m1", parameters=params)
    n.registry_entry = entry
    
    # 1. None
    status, res = await n.process_results(None)
    assert status == TaskStatus.FAILED
    assert res is None
    
    # 2. Bool True
    status, res = await n.process_results(True)
    assert status == TaskStatus.COMPLETED
    assert res is True
    
    # 3. Bool False
    status, res = await n.process_results(False)
    assert status == TaskStatus.FAILED
    assert res is None
    
    # 4. SimstackResult
    sim_res = SimstackResult(status=TaskStatus.COMPLETED)
    status, res = await n.process_results(sim_res)
    assert status == TaskStatus.COMPLETED
    assert res == sim_res
