import pytest
from simstack.core.context import context
from simstack.models.models import NodeModel, ModelMapping, Parameters
from simstack.util.importer import _find_node_model, _find_model_mapping

@pytest.mark.asyncio
async def test_find_node_model_exact_mapping(initialized_context):
    """Test _find_node_model finds a model by exact function_mapping."""
    node_model = NodeModel(
        name="test_func_exact",
        function_mapping="module.submodule.test_func_exact",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    await context.refresh_mappings()
    
    try:
        found = await _find_node_model("module.submodule.test_func_exact", context.db)
        assert found is not None
        assert found.id == node_model.id
        assert found.name == "test_func_exact"
    finally:
        await context.db.delete(node_model)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_node_model_name_fallback_with_dot(initialized_context):
    """Test _find_node_model falls back to name when mapping doesn't match."""
    node_model = NodeModel(
        name="test_func_fallback",
        function_mapping="original.path.test_func_fallback",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    await context.refresh_mappings()
    
    try:
        # Try finding with a different path but same function name
        found = await _find_node_model("new.path.test_func_fallback", context.db)
        assert found is not None
        assert found.id == node_model.id
        assert found.name == "test_func_fallback"
    finally:
        await context.db.delete(node_model)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_node_model_name_fallback_no_dot(initialized_context):
    """Test _find_node_model falls back to name when path has no dot."""
    node_model = NodeModel(
        name="test_func_simple",
        function_mapping="some.path.test_func_simple",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    await context.refresh_mappings()
    
    try:
        # Try finding with just the name
        found = await _find_node_model("test_func_simple", context.db)
        assert found is not None
        assert found.id == node_model.id
        assert found.name == "test_func_simple"
    finally:
        await context.db.delete(node_model)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_model_mapping_exact_mapping(initialized_context):
    """Test _find_model_mapping finds a mapping by exact mapping path."""
    model_mapping = ModelMapping(
        name="TestModelExact",
        mapping="module.submodule.TestModelExact",
        collection_name="test_collection_exact"
    )
    await context.db.save(model_mapping)
    await context.refresh_mappings()
    
    try:
        found = await _find_model_mapping("module.submodule.TestModelExact", context.db)
        assert found is not None
        assert found.id == model_mapping.id
        assert found.name == "TestModelExact"
    finally:
        await context.db.delete(model_mapping)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_model_mapping_name_fallback(initialized_context):
    """Test _find_model_mapping falls back to name when mapping doesn't match."""
    model_mapping = ModelMapping(
        name="TestModelFallback",
        mapping="original.path.TestModelFallback",
        collection_name="test_collection_fallback"
    )
    await context.db.save(model_mapping)
    await context.refresh_mappings()
    
    try:
        # Try finding with a different path but same class name
        found = await _find_model_mapping("new.path.TestModelFallback", context.db)
        assert found is not None
        assert found.id == model_mapping.id
        assert found.name == "TestModelFallback"
    finally:
        await context.db.delete(model_mapping)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_find_node_model_not_found(initialized_context):
    """Test _find_node_model returns None when not found."""
    found = await _find_node_model("nonexistent.path.NonExistentFunc", context.db)
    assert found is None

@pytest.mark.asyncio
async def test_find_model_mapping_not_found(initialized_context):
    """Test _find_model_mapping returns None when not found (actually it might raise ValueError if no dot, but let's see)."""
    # _find_model_mapping uses model_path.rsplit(".", 1), so it needs a dot
    found = await _find_model_mapping("nonexistent.path.NonExistentModel", context.db)
    assert found is None
