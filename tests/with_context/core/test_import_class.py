from server import storage_criteria
from server.storage_models import StoredModelMapping
import pytest
from simstack.core.context import context
from simstack.models.document import Model

from simstack.util.importer import import_class


class SampleClass(Model):
    value: str = "default"

    def get_value(self):
        return self.value


class AnotherSampleClass(Model):
    name: str = "default"

    def get_name(self):
        return self.name


@pytest.mark.asyncio
async def test_import_class_regular(initialized_context):
    cls = await import_class(f"{__name__}.SampleClass", context.server.values)
    assert cls is SampleClass
    assert cls(value="test").get_value() == "test"


@pytest.mark.asyncio
async def test_import_class_from_model_mapping(initialized_context):
    cls = await import_class("historical.module.SampleClass", context.server.values)
    assert cls is SampleClass
    assert cls(value="test").get_value() == "test"


@pytest.mark.asyncio
async def test_import_class_bare_name_mapping_keeps_class_path_module(initialized_context, server_storage):
    descriptor = await server_storage.find_one(StoredModelMapping, storage_criteria.field(StoredModelMapping, 'name') == "SampleClass")
    descriptor.mapping = "SampleClass"
    await server_storage.save(descriptor)
    cls = await import_class(f"{__name__}.SampleClass", context.server.values)
    assert cls is SampleClass
    assert cls(value="bare").get_value() == "bare"


@pytest.mark.asyncio
async def test_import_class_nonexistent(initialized_context):
    with pytest.raises(LookupError, match="NonExistentClass.*revision catalogue"):
        await import_class(f"{__name__}.NonExistentClass", context.server.values)
