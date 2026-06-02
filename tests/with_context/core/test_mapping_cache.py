import pytest
from odmantic import Model

from simstack.core.context import context
from simstack.models.models import ModelMapping, NodeModel, Parameters
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


async def _delete_saved_model(saved_model):
    try:
        await context.db.delete(saved_model)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_import_class_refreshes_stale_model_mapping_cache(initialized_context):
    class_name = "LateModelMappingCacheByPath"
    model_class = _install_late_model(class_name)
    mapping_path = f"{__name__}.{class_name}"
    model_mapping = ModelMapping(
        name=class_name,
        mapping=mapping_path,
        collection_name="late_model_mapping_cache_by_path",
    )

    await context.refresh_mappings(models=True, nodes=False)
    assert context.model_mappings.get_by_name(class_name) is None

    try:
        await context.db.save(model_mapping)
        assert context.model_mappings.get_by_name(class_name) is None

        imported_class = await import_class(mapping_path, context.db)

        assert imported_class is model_class
        assert context.model_mappings.get_by_name(class_name) is not None
    finally:
        await _delete_saved_model(model_mapping)
        globals().pop(class_name, None)
        await context.refresh_mappings(models=True, nodes=False)


@pytest.mark.asyncio
async def test_import_class_by_name_refreshes_stale_model_mapping_cache():
    class_name = "LateModelMappingCacheByName"
    model_class = _install_late_model(class_name)
    mapping_path = f"{__name__}.{class_name}"
    model_mapping = ModelMapping(
        name=class_name,
        mapping=mapping_path,
        collection_name="late_model_mapping_cache_by_name",
    )

    await context.refresh_mappings(models=True, nodes=False)
    assert context.model_mappings.get_by_name(class_name) is None
    db = context.db
    try:
        await context.db.save(model_mapping)
        assert context.model_mappings.get_by_name(class_name) is None

        imported_class = await import_class_by_name(class_name, db)

        assert imported_class is model_class
        assert context.model_mappings.get_by_name(class_name) is not None
    finally:
        await _delete_saved_model(model_mapping)
        globals().pop(class_name, None)
        await context.refresh_mappings(models=True, nodes=False)


@pytest.mark.asyncio
async def test_import_function_refreshes_stale_node_mapping_cache():
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
        await context.db.save(node_model)
        assert context.node_mappings.get_by_mapping(function_mapping) is None

        imported_function = await import_function(function_mapping, context.db)

        assert imported_function is function
        assert imported_function("fresh") == "fresh"
        assert context.node_mappings.get_by_mapping(function_mapping) is not None
    finally:
        await _delete_saved_model(node_model)
        globals().pop(function_name, None)
        await context.refresh_mappings(models=False, nodes=True)


@pytest.mark.asyncio
async def test_import_function_by_name_refreshes_stale_node_mapping_cache():
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
        await context.db.save(node_model)
        assert context.node_mappings.get_by_name(function_name) is None

        imported_function = await import_function_by_name(function_name, db=context.db, task_id=None)

        assert imported_function is function
        assert imported_function("fresh") == "fresh"
        assert context.node_mappings.get_by_name(function_name) is not None
    finally:
        await _delete_saved_model(node_model)
        globals().pop(function_name, None)
        await context.refresh_mappings(models=False, nodes=True)


@pytest.mark.asyncio
async def test_context_mapping_cache_is_populated_after_table_rebuild_setup():
    assert context.model_mappings.get_by_name("FloatData") is not None
    assert context.node_mappings.get_by_name("adder_in_tests") is not None
