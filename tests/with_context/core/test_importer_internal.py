from server.storage_models import StoredModelMapping
import pytest
from simstack.core.context import context
from simstack.models.models import NodeModel, Parameters
from simstack.util.importer import _find_node_model, _find_model_mapping

@pytest.mark.asyncio
async def test_find_node_model_exact_mapping(initialized_context, server_storage):
    """Test _find_node_model finds a model by exact function_mapping."""
    node_model = NodeModel(
        name="test_func_exact",
        function_mapping="module.submodule.test_func_exact",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await server_storage.save(node_model)
    await context.refresh_mappings()
    
    try:
        found = await _find_node_model("module.submodule.test_func_exact", context.server.values)
        assert found is not None
        assert found.id == node_model.id
        assert found.name == "test_func_exact"
    finally:
        await server_storage.delete(node_model)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_node_model_name_fallback_with_dot(initialized_context, server_storage):
    """Test _find_node_model falls back to name when mapping doesn't match."""
    node_model = NodeModel(
        name="test_func_fallback",
        function_mapping="original.path.test_func_fallback",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await server_storage.save(node_model)
    await context.refresh_mappings()
    
    try:
        # Try finding with a different path but same function name
        found = await _find_node_model("new.path.test_func_fallback", context.server.values)
        assert found is not None
        assert found.id == node_model.id
        assert found.name == "test_func_fallback"
    finally:
        await server_storage.delete(node_model)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_node_model_name_fallback_no_dot(initialized_context, server_storage):
    """Test _find_node_model falls back to name when path has no dot."""
    node_model = NodeModel(
        name="test_func_simple",
        function_mapping="some.path.test_func_simple",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await server_storage.save(node_model)
    await context.refresh_mappings()
    
    try:
        # Try finding with just the name
        found = await _find_node_model("test_func_simple", context.server.values)
        assert found is not None
        assert found.id == node_model.id
        assert found.name == "test_func_simple"
    finally:
        await server_storage.delete(node_model)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_model_mapping_exact_mapping(initialized_context, server_storage):
    """Test _find_model_mapping finds a mapping by exact mapping path."""
    model_mapping = StoredModelMapping(
        name="TestModelExact",
        mapping="module.submodule.TestModelExact",
        collection_name="test_collection_exact"
    )
    await server_storage.save(model_mapping)
    await context.refresh_mappings()
    
    try:
        found = await _find_model_mapping("module.submodule.TestModelExact", context.server.values)
        assert found is not None
        assert found.id == model_mapping.id
        assert found.name == "TestModelExact"
    finally:
        await server_storage.delete(model_mapping)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_model_mapping_name_fallback(initialized_context, server_storage):
    """Test _find_model_mapping falls back to name when mapping doesn't match."""
    model_mapping = StoredModelMapping(
        name="TestModelFallback",
        mapping="original.path.TestModelFallback",
        collection_name="test_collection_fallback"
    )
    await server_storage.save(model_mapping)
    await context.refresh_mappings()
    
    try:
        # Try finding with a different path but same class name
        found = await _find_model_mapping("new.path.TestModelFallback", context.server.values)
        assert found is not None
        assert found.id == model_mapping.id
        assert found.name == "TestModelFallback"
    finally:
        await server_storage.delete(model_mapping)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_node_model_not_found(initialized_context):
    """Test _find_node_model returns None when not found."""
    found = await _find_node_model("nonexistent.path.NonExistentFunc", context.server.values)
    assert found is None

@pytest.mark.asyncio
async def test_find_model_mapping_not_found(initialized_context):
    """Test _find_model_mapping returns None when not found."""
    found = await _find_model_mapping("nonexistent.path.NonExistentModel", context.server.values)
    assert found is None


@pytest.mark.asyncio
async def test_find_model_mapping_name_only_path(initialized_context, server_storage):
    """Bare class names must not crash rsplit when looking up ModelMapping."""
    model_mapping = StoredModelMapping(
        name="BareNameModel",
        mapping="BareNameModel",
        collection_name="bare_name_collection",
    )
    await server_storage.save(model_mapping)
    await context.refresh_mappings()
    try:
        found = await _find_model_mapping("BareNameModel", context.server.values)
        assert found is not None
        assert found.id == model_mapping.id
    finally:
        await server_storage.delete(model_mapping)
        await context.refresh_mappings()
