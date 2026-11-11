from server.storage_models import StoredModelMapping
import pytest
from tests.with_context.core.test_simple_nodes import adder_in_tests
from simstack.models.document import Model

from simstack.core.context import context
from simstack.models.models import NodeModel, Parameters
from simstack.util.importer import (
    import_class,
    import_class_by_name,
    import_function,
    import_function_by_name,
)


def _install_late_model(class_name: str) -> type[Model]:
    model_class = type(
        class_name,
        (Model,),
        {
            "__module__": __name__,
            "__annotations__": {"value": str},
            "value": "default",
        },
    )
    globals()[class_name] = model_class
    return model_class


def _install_late_function(function_name: str):
    def late_function(value=None):
        return value

    late_function.__name__ = function_name
    late_function.__module__ = __name__
    globals()[function_name] = late_function
    return late_function




@pytest.mark.asyncio
async def test_import_class_refreshes_stale_model_mapping_cache(initialized_context, server_storage):
    class_name = "LateModelMappingCacheByPath"
    model_class = _install_late_model(class_name)
    mapping_path = f"{__name__}.{class_name}"
    model_mapping = StoredModelMapping(
        name=class_name,
        mapping=mapping_path,
        collection_name="late_model_mapping_cache_by_path",
    )

    await context.refresh_mappings(models=True, nodes=False)
    assert context.model_mappings.get_by_name(class_name) is None

    try:
        await server_storage.save(model_mapping)
        assert context.model_mappings.get_by_name(class_name) is None

        imported_class = await import_class(mapping_path, context.server.values)

        assert imported_class is model_class
        assert context.model_mappings.get_by_name(class_name) is not None
    finally:
        await server_storage.delete(model_mapping)
        globals().pop(class_name, None)
        await context.refresh_mappings(models=True, nodes=False)


@pytest.mark.asyncio
async def test_import_class_by_name_refreshes_stale_model_mapping_cache(server_storage):
    class_name = "LateModelMappingCacheByName"
    model_class = _install_late_model(class_name)
    mapping_path = f"{__name__}.{class_name}"
    model_mapping = StoredModelMapping(
        name=class_name,
        mapping=mapping_path,
        collection_name="late_model_mapping_cache_by_name",
    )

    await context.refresh_mappings(models=True, nodes=False)
    assert context.model_mappings.get_by_name(class_name) is None
    values = context.server.values
    try:
        await server_storage.save(model_mapping)
        assert context.model_mappings.get_by_name(class_name) is None

        imported_class = await import_class_by_name(class_name, values)

        assert imported_class is model_class
        assert context.model_mappings.get_by_name(class_name) is not None
    finally:
        await server_storage.delete(model_mapping)
        globals().pop(class_name, None)
        await context.refresh_mappings(models=True, nodes=False)


@pytest.mark.asyncio
async def test_import_function_refreshes_stale_node_mapping_cache(server_storage):
    function_name = "late_node_mapping_cache_by_path"
    function = _install_late_function(function_name)
    function_mapping = f"{__name__}.{function_name}"
    node_model = NodeModel(
        name=function_name,
        function_mapping=function_mapping,
        description="Late node mapping cache test",
        input_mappings=[],
        default_parameters=Parameters(),
    )

    await context.refresh_mappings(models=False, nodes=True)
    assert context.node_mappings.get_by_mapping(function_mapping) is None

    try:
        await server_storage.save(node_model)
        assert context.node_mappings.get_by_mapping(function_mapping) is None

        imported_function = await import_function(function_mapping, context.server.values)

        assert imported_function is function
        assert imported_function("fresh") == "fresh"
        assert context.node_mappings.get_by_mapping(function_mapping) is not None
    finally:
        await server_storage.delete(node_model)
        globals().pop(function_name, None)
        await context.refresh_mappings(models=False, nodes=True)


@pytest.mark.asyncio
async def test_import_function_by_name_refreshes_stale_node_mapping_cache(server_storage):
    function_name = "late_node_mapping_cache_by_name"
    function = _install_late_function(function_name)
    function_mapping = f"{__name__}.{function_name}"
    node_model = NodeModel(
        name=function_name,
        function_mapping=function_mapping,
        description="Late node mapping cache by name test",
        input_mappings=[],
        default_parameters=Parameters(),
    )

    await context.refresh_mappings(models=False, nodes=True)
    assert context.node_mappings.get_by_name(function_name) is None

    try:
        await server_storage.save(node_model)
        assert context.node_mappings.get_by_name(function_name) is None

        imported_function = await import_function_by_name(function_name, api=context.server.values, task_id=None)

        assert imported_function is function
        assert imported_function("fresh") == "fresh"
        assert context.node_mappings.get_by_name(function_name) is not None
    finally:
        await server_storage.delete(node_model)
        globals().pop(function_name, None)
        await context.refresh_mappings(models=False, nodes=True)


@pytest.mark.asyncio
async def test_context_mapping_cache_is_populated_after_table_rebuild_setup():
    assert context.model_mappings.get_by_name("FloatData") is not None
    assert context.node_mappings.get_by_name("adder_in_tests") is not None
