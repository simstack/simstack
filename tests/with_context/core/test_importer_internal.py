import pytest

from simstack.models.models import ModelMapping, NodeModel, Parameters
from simstack.util.importer import _find_model_mapping, _find_node_model


@pytest.mark.asyncio
async def test_find_node_model_uses_exact_mapping(repository):
    node_model = NodeModel(
        name="test_func_exact",
        function_mapping="module.submodule.test_func_exact",
        input_mappings=[],
        default_parameters=Parameters(),
    )
    await repository.save(node_model)

    try:
        found = await _find_node_model(
            "module.submodule.test_func_exact", repository
        )
        assert found is not None
        assert found.id == node_model.id
    finally:
        await repository.delete(node_model)


@pytest.mark.asyncio
async def test_find_node_model_does_not_fall_back_to_name(repository):
    node_model = NodeModel(
        name="test_func_no_fallback",
        function_mapping="original.path.test_func_no_fallback",
        input_mappings=[],
        default_parameters=Parameters(),
    )
    await repository.save(node_model)

    try:
        assert (
            await _find_node_model("new.path.test_func_no_fallback", repository)
            is None
        )
        assert await _find_node_model("test_func_no_fallback", repository) is None
    finally:
        await repository.delete(node_model)


@pytest.mark.asyncio
async def test_find_model_mapping_uses_exact_mapping(repository):
    model_mapping = ModelMapping(
        name="TestModelExact",
        mapping="module.submodule.TestModelExact",
        collection_name="test_collection_exact",
    )
    await repository.save(model_mapping)

    try:
        found = await _find_model_mapping(
            "module.submodule.TestModelExact", repository
        )
        assert found is not None
        assert found.id == model_mapping.id
    finally:
        await repository.delete(model_mapping)


@pytest.mark.asyncio
async def test_find_model_mapping_does_not_fall_back_to_name(repository):
    model_mapping = ModelMapping(
        name="TestModelNoFallback",
        mapping="original.path.TestModelNoFallback",
        collection_name="test_collection_no_fallback",
    )
    await repository.save(model_mapping)

    try:
        assert (
            await _find_model_mapping("new.path.TestModelNoFallback", repository)
            is None
        )
    finally:
        await repository.delete(model_mapping)


@pytest.mark.asyncio
async def test_exact_mapping_lookups_return_none_when_absent(repository):
    assert (
        await _find_node_model("nonexistent.path.NonExistentFunc", repository)
        is None
    )
    assert (
        await _find_model_mapping("nonexistent.path.NonExistentModel", repository)
        is None
    )
