import json
from types import ModuleType

import pytest

from simstack.models.document import Model, EmbeddedModel
from simstack.models.models import ModelMapping
from simstack.persistence import eq
from simstack.tables.model_table import CreateModelTable
from tests.support.in_memory_repository import InMemoryRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [Model, EmbeddedModel])
async def test_plain_domain_model_registration_always_publishes_wire_schema(base):
    module = ModuleType("plain_workflow")
    number = type("Number", (base,), {"__module__": module.__name__, "__annotations__": {"value": int}})
    module.Number = number
    repository = InMemoryRepository()
    await CreateModelTable(repository)._create_models_from_module(module, drops="")
    mapping = await repository.find_one(ModelMapping, eq("mapping", "plain_workflow.Number"))
    assert mapping is not None
    assert mapping.json_schema is not None
    schema = json.loads(mapping.json_schema)
    assert schema["properties"]["value"]["type"] == "integer"
    assert "value" in schema["required"]
