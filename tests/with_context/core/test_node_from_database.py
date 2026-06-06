import pytest
import pytest_asyncio
from odmantic import ObjectId
from simstack.core.context import context
from simstack.core.node import node, node_from_database
from simstack.models import FloatData, BinaryOperationInput, NodeRegistry, Parameters, ModelMapping, NodeModel
from simstack.core.definitions import TaskStatus

@node()
def helper_node_func(args: FloatData, **kwargs) -> FloatData:
    return FloatData(value=args.value + 1)

@pytest_asyncio.fixture
async def setup_helper_node_model(initialized_context):
    """Fixture to ensure helper_node_func mapping exists."""
    node_model = NodeModel(
        name="helper_node_func",
        function_mapping="simstack_tests.with_context.core.test_node_from_database.helper_node_func",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    await context.refresh_mappings()
    
    yield node_model
    
    await context.db.delete(node_model)
    await context.refresh_mappings()

@pytest.mark.asyncio
async def test_node_from_database_basic(initialized_context, setup_helper_node_model):
    """Test reconstructing a Node from a NodeRegistry entry in the database."""
    
    # 2. Create input data and save it
    input_data = FloatData(value=10.0)
    await context.db.save(input_data)
    
    try:
        # 3. Create a NodeRegistry entry
        parameters = Parameters()
        
        registry_entry = NodeRegistry(
            name="helper_node_func",
            status=TaskStatus.SUBMITTED,
            input_tables=["simstack.models.FloatData"],
            input_ids=[input_data.id],
            function_hash="NOT INITIALIZED",
            arg_hash="NOT INITIALIZED",
            func_mapping="simstack_tests.with_context.core.test_node_from_database.helper_node_func",
            parameters=parameters,
            is_async=False
        )
        await context.db.save(registry_entry)
        
        try:
            # 4. Call node_from_database
            reconstructed_node = await node_from_database(registry_entry)
            
            # 5. Assertions
            assert reconstructed_node is not None
            assert reconstructed_node.name == "helper_node_func"
            assert len(reconstructed_node._args) == 1
            assert reconstructed_node._args[0].value == 10.0
            assert reconstructed_node.registry_entry.id == registry_entry.id
            
            # Verify that hashes were initialized
            assert registry_entry.function_hash != "NOT INITIALIZED"
            assert registry_entry.arg_hash != "NOT INITIALIZED"
        finally:
            await context.db.delete(registry_entry)
    finally:
        await context.db.delete(input_data)

@pytest.mark.asyncio
async def test_node_from_database_signature_fix(initialized_context, setup_helper_node_model):
    """Specifically test that import_function is called correctly when cache is bypassed."""
    input_data = FloatData(value=5.0)
    await context.db.save(input_data)

    try:
        # Clear cache to force DB lookup in _find_node_model
        context._node_mappings = None

        registry_entry = NodeRegistry(
            name="helper_node_func",
            status=TaskStatus.SUBMITTED,
            input_tables=["simstack.models.FloatData"],
            input_ids=[input_data.id],
            function_hash="NOT INITIALIZED",
            arg_hash="NOT INITIALIZED",
            func_mapping="simstack_tests.with_context.core.test_node_from_database.helper_node_func",
            parameters=Parameters(),
            is_async=False
        )
        await context.db.save(registry_entry)

        try:
            # Now call node_from_database.
            reconstructed_node = await node_from_database(registry_entry)
            assert reconstructed_node is not None
            assert reconstructed_node.name == "helper_node_func"
            assert len(reconstructed_node._args) == 1
            assert reconstructed_node._args[0].value == 5.0
        finally:
            await context.db.delete(registry_entry)
    finally:
        await context.db.delete(input_data)

@pytest.mark.asyncio
async def test_node_from_database_duplicate(initialized_context, setup_helper_node_model):
    """Test that node_from_database handles duplicate entries correctly."""
    
    input_data = FloatData(value=20.0)
    await context.db.save(input_data)
    
    try:
        parameters = Parameters()
        
        # Create an EXISTING entry in the database that is already initialized
        # We need to know the hashes to create a duplicate
        from simstack.core.node import compute_arg_hash
        from simstack.core.hash import complex_hash_function
        
        arg_hash = compute_arg_hash([input_data])
        func_hash = complex_hash_function(helper_node_func._inner)
        
        existing_entry = NodeRegistry(
            name="helper_node_func",
            status=TaskStatus.COMPLETED,
            input_tables=["simstack.models.FloatData"],
            input_ids=[input_data.id],
            function_hash=func_hash,
            arg_hash=arg_hash,
            func_mapping="simstack_tests.with_context.core.test_node_from_database.helper_node_func",
            parameters=parameters,
            is_async=False
        )
        await context.db.save(existing_entry)
        
        try:
            # Now create a NEW entry that is NOT INITIALIZED
            new_registry_entry = NodeRegistry(
                name="helper_node_func",
                status=TaskStatus.SUBMITTED,
                input_tables=["simstack.models.FloatData"],
                input_ids=[input_data.id],
                function_hash="NOT INITIALIZED",
                arg_hash="NOT INITIALIZED",
                func_mapping="simstack_tests.with_context.core.test_node_from_database.helper_node_func",
                parameters=parameters,
                is_async=False
            )
            await context.db.save(new_registry_entry)
            new_entry_id = new_registry_entry.id
            
            try:
                # Call node_from_database
                reconstructed_node = await node_from_database(new_registry_entry)
                
                # Assertions
                assert reconstructed_node is not None
                # It should have recovered the existing entry
                assert reconstructed_node.registry_entry.id == existing_entry.id
                assert reconstructed_node.registry_entry.status == TaskStatus.COMPLETED
                
                # The new entry should have been deleted
                deleted_entry = await context.db.find_one(NodeRegistry, NodeRegistry.id == new_entry_id)
                assert deleted_entry is None
            finally:
                # new_registry_entry might have been deleted by node_from_database
                # but existing_entry definitely needs deletion
                pass
        finally:
            await context.db.delete(existing_entry)
    finally:
        await context.db.delete(input_data)

@pytest.mark.asyncio
async def test_node_from_database_invalid_mapping_with_duplicate(initialized_context, setup_helper_node_model):
    """Test that node_from_database can still recover a duplicate even if the mapping is invalid."""
    
    input_data = FloatData(value=30.0)
    await context.db.save(input_data)
    
    try:
        from simstack.core.node import compute_arg_hash
        from simstack.core.hash import complex_hash_function
        
        arg_hash = compute_arg_hash([input_data])
        func_hash = complex_hash_function(helper_node_func._inner)
        
        # Create an EXISTING entry that is COMPLETED
        existing_entry = NodeRegistry(
            name="helper_node_func",
            status=TaskStatus.COMPLETED,
            input_tables=["simstack.models.FloatData"],
            input_ids=[input_data.id],
            function_hash=func_hash,
            arg_hash=arg_hash,
            func_mapping="simstack_tests.with_context.core.test_node_from_database.helper_node_func",
            parameters=Parameters(),
            is_async=False
        )
        await context.db.save(existing_entry)
        
        try:
            # Now create a NEW entry with an INVALID mapping but CORRECT hashes
            new_registry_entry = NodeRegistry(
                name="helper_node_func",
                status=TaskStatus.SUBMITTED,
                input_tables=["simstack.models.FloatData"],
                input_ids=[input_data.id],
                function_hash=func_hash, # Pre-initialized hashes
                arg_hash=arg_hash,
                func_mapping="non_existent_module.func", # INVALID MAPPING
                parameters=Parameters(),
                is_async=False
            )
            await context.db.save(new_registry_entry)
            
            # Call node_from_database
            # This is expected to fail currently because import_function will raise ModuleNotFoundError
            reconstructed_node = await node_from_database(new_registry_entry)
            
            assert reconstructed_node is not None
            assert reconstructed_node.registry_entry.id == existing_entry.id
            
        finally:
            await context.db.delete(existing_entry)
    finally:
        await context.db.delete(input_data)
