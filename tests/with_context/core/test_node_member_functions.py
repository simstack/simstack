import pytest
import os
import asyncio
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from odmantic import Model, ObjectId
from simstack.core.context import context
from simstack.core.node import Node, node, node_from_database
from simstack.models import FloatData, Parameters, NodeRegistry, ModelMapping, NodeModel
from simstack.core.definitions import TaskStatus
from simstack.core.simstack_result import SimstackResult
from simstack.models.files import FileStack
from simstack.models.parameters import Resource, Queue, SlurmParameters
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

@pytest_asyncio.fixture
async def setup_mappings(initialized_context):
    """Fixture to set up standard mappings for tests."""
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
    
    yield
    
    await context.db.delete(node_model)
    await context.db.delete(model_mapping)
    await context.refresh_mappings()

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
async def test_make_registry_entry_success(initialized_context, setup_mappings):
    """Test successful creation of registry entry."""
    data = FloatData(value=1.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
    
    entry = await n.make_registry_entry("func_hash", "arg_hash")
    try:
        assert entry is not None
        assert entry.name == "sync_node"
        assert entry.function_hash == "func_hash"
        assert entry.arg_hash == "arg_hash"
        assert n.registry_entry == entry
        assert len(entry.input_references) == 1
    finally:
        await context.db.delete(entry)
        await context.db.delete(data)


@pytest.mark.asyncio
async def test_make_registry_entry_inherits_parent_slurm_on_self(
    initialized_context, setup_mappings
):
    data = FloatData(value=1.0)
    parent_parameters = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, tasks=2, mem="8G"),
    )
    n = Node(
        data,
        func=sync_node._inner,
        is_async=False,
        parameters=Parameters(resource="self"),
        parent_parameters=parent_parameters,
    )

    entry = await n.make_registry_entry("func_hash", "arg_hash")
    try:
        assert entry.parameters.resource == "self"
        assert entry.parameters.slurm_parameters.cpus_per_task == 4
        assert entry.parameters.slurm_parameters.tasks == 2
        assert entry.parameters.slurm_parameters.mem == "8G"
        assert n.parameters.slurm_parameters.mem == "8G"
    finally:
        await context.db.delete(entry)
        await context.db.delete(data)


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
    node_model = NodeModel(
        name="sync_node",
        function_mapping="simstack_tests.with_context.core.test_node_member_functions.sync_node",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    try:
        await context.refresh_mappings()
        with patch.object(context.model_mappings, "get_by_name", return_value=None):
            with patch.object(context.db, "find_one", side_effect=[node_model, None]):
                 with pytest.raises(ValueError, match="Could not find table name"):
                    await n.make_registry_entry("h1", "h2")
    finally:
        await context.db.delete(node_model)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_get_node_registry(initialized_context, setup_mappings):
    """Test get_node_registry including caching and force_rerun."""
    data = FloatData(value=1.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
    
    # 1. First call - should create new entry
    status = await n.get_node_registry()
    entry1 = n.registry_entry
    try:
        assert status == TaskStatus.SUBMITTED
        first_id = n.id
        
        # 2. Second call - should load from DB
        n2 = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
        status2 = await n2.get_node_registry()
        assert status2 == TaskStatus.SUBMITTED
        assert n2.id == first_id

        # 3. Changing the implementation must not invalidate the cached task:
        # function identity is versioned by git, not by hashing the body.
        def changed_sync_node(data: FloatData, **kwargs) -> FloatData:
            return FloatData(value=data.value + 99.0)

        changed_sync_node.__name__ = "sync_node"
        changed = Node(
            data,
            func=changed_sync_node,
            is_async=False,
            parameters=Parameters(),
        )
        status_changed = await changed.get_node_registry()
        changed_entry = changed.registry_entry
        assert status_changed == TaskStatus.SUBMITTED
        assert changed.id == first_id
        assert changed_entry is not None
        assert changed_entry.id == entry1.id
        assert changed_entry.function_hash == ""
        assert changed_entry.function_hash == entry1.function_hash

        # 4. Call with force_rerun - should create new entry
        params_rerun = Parameters(force_rerun=True)
        n3 = Node(data, func=sync_node._inner, is_async=False, parameters=params_rerun)
        status3 = await n3.get_node_registry()
        entry3 = n3.registry_entry
        try:
            assert status3 == TaskStatus.SUBMITTED
            assert n3.id != first_id
        finally:
            if entry3:
                await context.db.delete(entry3)
    finally:
        if entry1:
            await context.db.delete(entry1)
        await context.db.delete(data)

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
    # A completed result with no references represents a successful bool node.
    res = await n.load_results()
    assert res is True
    
    # 3. Successful load
    res_data = FloatData(value=5.0)
    await context.db.save(res_data)
    
    # Re-setup mappings and refresh
    model_mapping = ModelMapping(name="FloatData", mapping="simstack.models.FloatData", collection_name="float_data")
    await context.db.save(model_mapping)
    try:
        await context.refresh_mappings()
        
        from simstack.models.named_data_reference import NamedDataReference
        entry.status = TaskStatus.COMPLETED
        entry.results_references = [NamedDataReference(
            variable_name="output",
            variable_mapping="simstack.models.FloatData",
            reference=res_data.id
        )]
        await context.db.save(entry) # Ensure it's saved if load_results re-loads
        
        try:
            loaded_res = await n.load_results()
            assert loaded_res is not None
            assert isinstance(loaded_res, FloatData)
            assert loaded_res.value == 5.0
        finally:
            await context.db.delete(entry)
    finally:
        await context.db.delete(model_mapping)
        await context.db.delete(res_data)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_execute_node_locally_sync(initialized_context, setup_mappings):
    """Test local execution of a synchronous node."""
    data = FloatData(value=10.0)
    n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters())
    await n.get_node_registry()
    entry = n.registry_entry
    
    try:
        result = await n.execute_node_locally()
        assert isinstance(result, FloatData)
        assert result.value == 11.0
        assert n.status == TaskStatus.COMPLETED
    finally:
        if entry:
            # result is also saved to DB by process_results
            if entry.results_references:
                for ref in entry.results_references:
                    rid = ref.reference
                    # We need to find which model it is, but we know it's FloatData here
                    # Database has no delete_by_id, so we use find_one then delete
                    res_to_del = await context.db.find_one(FloatData, FloatData.id == rid)
                    if res_to_del:
                        await context.db.delete(res_to_del)
            await context.db.delete(entry)
        await context.db.delete(data)

@pytest.mark.asyncio
async def test_execute_node_locally_async(initialized_context, setup_mappings):
    """Test local execution of an asynchronous node."""
    data = FloatData(value=10.0)
    n = Node(data, func=async_node._inner, is_async=True, parameters=Parameters())
    await n.get_node_registry()
    entry = n.registry_entry
    
    try:
        result = await n.execute_node_locally()
        assert isinstance(result, FloatData)
        assert result.value == 12.0
        assert n.status == TaskStatus.COMPLETED
    finally:
        if entry:
            if entry.results_references:
                for ref in entry.results_references:
                    rid = ref.reference
                    res_to_del = await context.db.find_one(FloatData, FloatData.id == rid)
                    if res_to_del:
                        await context.db.delete(res_to_del)
            await context.db.delete(entry)
        await context.db.delete(data)

@pytest.mark.asyncio
async def test_execute_node_locally_failure(initialized_context, setup_mappings):
    """Test failure during local execution."""
    data = FloatData(value=10.0)
    n = Node(data, func=failing_node._inner, is_async=False, parameters=Parameters())
    await n.get_node_registry()
    entry = n.registry_entry
    
    try:
        with pytest.raises(RuntimeError, match="Intentional failure"):
            await n.execute_node_locally()
        
        assert n.status == TaskStatus.FAILED
    finally:
        if entry:
            await context.db.delete(entry)
        await context.db.delete(data)

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
    
    try:
        await n.set_status(TaskStatus.RUNNING)
        assert n.status == TaskStatus.RUNNING
        
        # Check DB
        updated_entry = await context.db.find_one(NodeRegistry, NodeRegistry.id == entry.id)
        assert updated_entry.status == TaskStatus.RUNNING
    finally:
        await context.db.delete(entry)

@pytest.mark.asyncio
async def test_run_somewhere_local(initialized_context, setup_mappings, monkeypatch):
    """Test run_somewhere routing to local execution."""
    # Add 'local' to allowed resources
    original_resources = allowed_resources.get_resources()
    allowed_resources._resources.append("local")
    
    # Save original context resource
    original_context_resource = context.config._resource_str
    context.config._resource_str = "local"
    
    try:
        data = FloatData(value=10.0)
        n = Node(data, func=sync_node._inner, is_async=False, parameters=Parameters(resource="local"))
        await n.get_node_registry()
        entry = n.registry_entry

        try:
            # mongomock is process-local; process parity is covered by the
            # subprocess protocol tests, while this test verifies local routing.
            expected = FloatData(value=11.0)
            run_process = AsyncMock(return_value=expected)
            monkeypatch.setattr(n, "run_node_as_process", run_process)
            result = await n.run_somewhere()
            assert result is expected
            run_process.assert_awaited_once_with()
        finally:
            if entry:
                if entry.results_references:
                    for ref in entry.results_references:
                        rid = ref.reference
                        res_to_del = await context.db.find_one(FloatData, FloatData.id == rid)
                        if res_to_del:
                            await context.db.delete(res_to_del)
                await context.db.delete(entry)
            await context.db.delete(data)
    finally:
        allowed_resources._resources = original_resources
        context.config._resource_str = original_context_resource

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


@pytest.mark.asyncio
async def test_process_results_persists_simstack_result_metadata_and_files(
    initialized_context, caplog
):
    parameters = Parameters()
    execution_node = Node(func=sync_node._inner, is_async=False, parameters=parameters)
    execution_node.registry_entry = NodeRegistry(
        name="sync_node",
        status=TaskStatus.RUNNING,
        function_hash="metadata-function-hash",
        arg_hash="metadata-arg-hash",
        func_mapping="tests.sync_node",
        parameters=parameters,
    )
    result_file = FileStack.from_string("result", "result.txt")
    info_file = FileStack.from_string("info", "info.txt")
    secret = "mongodb://metadata-user:metadata-password@db.internal/simstack"
    result = SimstackResult(
        status=TaskStatus.COMPLETED,
        custom_name="custom display name",
        message="finished with details",
        error_message=f"non-fatal diagnostic at {secret}",
        files=[result_file],
        info_files=[info_file],
        value=FloatData(value=12.0),
    )

    status, processed = await execution_node.process_results(result)

    assert status == TaskStatus.COMPLETED
    assert processed is result
    registry = execution_node.registry_entry
    assert registry.custom_name == "custom display name"
    assert registry.message == "finished with details"
    assert "non-fatal diagnostic" in registry.error
    assert secret not in registry.error
    assert "metadata-user" not in registry.error
    assert "metadata-password" not in caplog.text
    assert len(registry.info_files) == 1
    assert registry.info_files[0].name == "info.txt"
    assert {ref.variable_name for ref in registry.results_references} == {
        "files",
        "value",
    }

    loaded = await execution_node.load_results()
    assert isinstance(loaded, SimstackResult)
    assert loaded.custom_name == "custom display name"
    assert loaded.message == "finished with details"
    assert "non-fatal diagnostic" in (loaded.error_message or "")
    assert secret not in (loaded.error_message or "")
    assert loaded.info_files[0].name == "info.txt"
    assert loaded.files.elements == [result_file.id]


@pytest.mark.asyncio
async def test_node_from_database_persists_sanitized_import_failure(
    initialized_context, monkeypatch
):
    secret = "mongodb://import-user:import-password@db.internal/simstack"
    parameters = Parameters()
    registry = NodeRegistry(
        name="broken_import_node",
        status=TaskStatus.RETRIEVED,
        function_hash="broken-import-function-hash",
        arg_hash="broken-import-arg-hash",
        func_mapping="user_nodes.broken_import_node",
        parameters=parameters,
    )

    async def fail_import(*args, **kwargs):
        raise ImportError(f"cannot import node from {secret}")

    monkeypatch.setattr("simstack.core.node.import_function", fail_import)

    assert await node_from_database(registry) is None
    assert registry.status == TaskStatus.FAILED
    assert "cannot import node" in (registry.error or "")
    assert secret not in (registry.error or "")
    assert "import-user" not in (registry.error or "")
    assert "import-password" not in (registry.error or "")


@pytest.mark.asyncio
async def test_node_wrappers_sanitize_persisted_failure(monkeypatch):
    secret = "mongodb://wrapper-user:wrapper-password@db.internal/simstack"
    entries = []

    async def fake_get_node_registry(self):
        self.registry_entry = NodeRegistry(
            name=self.name,
            status=TaskStatus.RETRIEVED,
            parameters=self.parameters,
            func_mapping="test_mapping",
            function_hash="wrapper-function-hash",
            arg_hash="wrapper-arg-hash",
        )
        entries.append(self.registry_entry)
        return TaskStatus.RETRIEVED

    async def fake_run_somewhere(self):
        self.registry_entry.status = TaskStatus.FAILED
        self.registry_entry.error = f"child failed at {secret}"
        return None

    async def fake_find_one(*args, **kwargs):
        return entries[-1]

    monkeypatch.setattr(Node, "get_node_registry", fake_get_node_registry)
    monkeypatch.setattr(Node, "run_somewhere", fake_run_somewhere)
    monkeypatch.setattr(context.db, "find_one", fake_find_one)

    with pytest.raises(RuntimeError) as sync_error:
        sync_node(FloatData(value=1.0))
    with pytest.raises(RuntimeError) as async_error:
        await async_node(FloatData(value=1.0))

    for error in (sync_error.value, async_error.value):
        assert secret not in str(error)
        assert "wrapper-user" not in str(error)
        assert "wrapper-password" not in str(error)
