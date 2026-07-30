import pytest
import pytest_asyncio
import logging
from odmantic import ObjectId
from simstack.core.context import context
from simstack.core.node import node, node_from_database
from simstack.models import FloatData, BinaryOperationInput, NodeRegistry, Parameters, ModelMapping, NodeModel
from simstack.models.files import FileStack
from simstack.models.named_data_reference import NamedDataReference
from simstack.models.pandas_model import PandasModel
from simstack.core.definitions import TaskStatus

# Get current module path for dynamic function mapping
CURRENT_MODULE = __name__


@node()
def helper_node_func(args: FloatData, **kwargs) -> FloatData:
    return FloatData(value=args.value + 1)

@pytest_asyncio.fixture
async def setup_helper_node_model(initialized_context):
    """Fixture to ensure helper_node_func mapping exists."""

    node_model = NodeModel(
        name="helper_node_func",
        function_mapping=f"{CURRENT_MODULE}.helper_node_func",
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
            input_references=[NamedDataReference.from_variable(input_data)],
            function_hash="NOT INITIALIZED",
            arg_hash="NOT INITIALIZED",
            func_mapping=f"{CURRENT_MODULE}.helper_node_func",
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
            input_references=[NamedDataReference.from_variable(input_data)],
            function_hash="NOT INITIALIZED",
            arg_hash="NOT INITIALIZED",
            func_mapping=f"{CURRENT_MODULE}.helper_node_func",
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
            input_references=[NamedDataReference.from_variable(input_data)],
            function_hash=func_hash,
            arg_hash=arg_hash,
            func_mapping=f"{CURRENT_MODULE}.helper_node_func",
            parameters=parameters,
            is_async=False
        )
        await context.db.save(existing_entry)
        
        try:
            # Now create a NEW entry that is NOT INITIALIZED
            new_registry_entry = NodeRegistry(
                name="helper_node_func",
                status=TaskStatus.SUBMITTED,
                input_references=[NamedDataReference.from_variable(input_data)],
                function_hash="NOT INITIALIZED",
                arg_hash="NOT INITIALIZED",
                func_mapping=f"{CURRENT_MODULE}.helper_node_func",
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
            input_references=[NamedDataReference.from_variable(input_data)],
            function_hash=func_hash,
            arg_hash=arg_hash,
            func_mapping=f"{CURRENT_MODULE}.helper_node_func",
            parameters=Parameters(),
            is_async=False
        )
        await context.db.save(existing_entry)
        
        try:
            # Now create a NEW entry with an INVALID mapping but CORRECT hashes
            new_registry_entry = NodeRegistry(
                name="helper_node_func",
                status=TaskStatus.SUBMITTED,
                input_references=[NamedDataReference.from_variable(input_data)],
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

@pytest.mark.asyncio
async def test_node_from_database_recovers_itself(initialized_context, setup_helper_node_model, caplog):
    """
    Test that node_from_database might 'recover itself' if hashes are pre-initialized 
    and it is already in the database.
    """
    input_data = FloatData(value=10.0)
    await context.db.save(input_data)
    
    from simstack.core.node import compute_arg_hash
    from simstack.core.hash import complex_hash_function
    
    arg_hash = compute_arg_hash([input_data])
    func_hash = complex_hash_function(helper_node_func._inner)
    
    # Create a registry entry that IS ALREADY in the DB and HAS HASHES
    existing_entry = NodeRegistry(
        name="helper_node_func",
        status=TaskStatus.SUBMITTED,
        input_references=[NamedDataReference.from_variable(input_data)],
        function_hash=func_hash,
        arg_hash=arg_hash,
        func_mapping=f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(),
        is_async=False
    )
    await context.db.save(existing_entry)
    
    # NEW entry with DIFFERENT ID but SAME hashes
    new_entry = NodeRegistry(
        name="helper_node_func",
        status=TaskStatus.SUBMITTED,
        input_references=[NamedDataReference.from_variable(input_data)],
        function_hash=func_hash,
        arg_hash=arg_hash,
        func_mapping=f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(),
        is_async=False
    )
    # SAVE it so it can be deleted
    await context.db.save(new_entry)
    
    try:
        # Call node_from_database with new_entry
        with caplog.at_level(logging.INFO, logger="Node"):
            reconstructed_node = await node_from_database(new_entry)
        
        assert reconstructed_node is not None
        assert reconstructed_node.registry_entry.id == existing_entry.id
        
        # Check if the INFO log about duplicate is present (since it's already in DB)
        assert "found duplicate entry" in caplog.text
        # Check that the error log is GONE (historical check)
        assert "recovered itself. This should not happen" not in caplog.text
        
    finally:
        await context.db.delete(existing_entry)
        await context.db.delete(input_data)


@pytest.mark.asyncio
async def test_node_from_database_hydrates_embedded_filestack(
    initialized_context, setup_helper_node_model
):
    canonical = FileStack(
        name="D3_unexpected.xyz",
        size=5557,
        is_hashable=False,
        in_memory=True,
        content=b"canonical-content",
    )
    await context.db.save(canonical)

    input_id = ObjectId()
    collection = context.db.raw_database[PandasModel.__collection__]
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
        function_hash="NOT INITIALIZED",
        arg_hash="NOT INITIALIZED",
        func_mapping=f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(),
    )
    await context.db.save(registry_entry)

    try:
        reconstructed_node = await node_from_database(registry_entry)

        assert reconstructed_node is not None
        hydrated = reconstructed_node._args[0].file_stack
        assert hydrated is not None
        assert hydrated.id == canonical.id
        assert hydrated.in_memory is True
        assert hydrated.content == canonical.content
        assert hydrated.locations == canonical.locations

        stored = await collection.find_one({"_id": input_id})
        assert stored["file_stack"]["in_memory"] is False
        assert stored["file_stack"]["content"] is None
        assert stored["file_stack"]["locations"] == []
    finally:
        await context.db.delete(registry_entry)
        await collection.delete_one({"_id": input_id})
        await context.db.delete(canonical)


@pytest.mark.asyncio
async def test_node_from_database_keeps_complete_embedded_filestack(
    initialized_context, setup_helper_node_model
):
    embedded_id = ObjectId()
    input_id = ObjectId()
    collection = context.db.raw_database[PandasModel.__collection__]
    await collection.insert_one(
        {
            "_id": input_id,
            "field_name": f"complete_embedded_filestack_{input_id}",
            "content_": b"",
            "file_stack": {
                "name": "embedded.xyz",
                "size": 7,
                "is_hashable": False,
                "hash": None,
                "in_memory": True,
                "content": b"content",
                "locations": [],
                "id": str(embedded_id),
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
        function_hash="NOT INITIALIZED",
        arg_hash="NOT INITIALIZED",
        func_mapping=f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(),
    )
    await context.db.save(registry_entry)

    try:
        reconstructed_node = await node_from_database(registry_entry)

        assert reconstructed_node is not None
        embedded = reconstructed_node._args[0].file_stack
        assert embedded is not None
        assert embedded.id == embedded_id
        assert embedded.content == b"content"
    finally:
        await context.db.delete(registry_entry)
        await collection.delete_one({"_id": input_id})


@pytest.mark.asyncio
async def test_node_from_database_rejects_missing_embedded_filestack(
    initialized_context, setup_helper_node_model, caplog
):
    missing_id = ObjectId()
    input_id = ObjectId()
    collection = context.db.raw_database[PandasModel.__collection__]
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
        function_hash="NOT INITIALIZED",
        arg_hash="NOT INITIALIZED",
        func_mapping=f"{CURRENT_MODULE}.helper_node_func",
        parameters=Parameters(),
    )
    await context.db.save(registry_entry)

    try:
        reconstructed_node = await node_from_database(registry_entry)

        assert reconstructed_node is None
        assert f"Referenced FileStack {missing_id} not found" in caplog.text
    finally:
        await context.db.delete(registry_entry)
        await collection.delete_one({"_id": input_id})
