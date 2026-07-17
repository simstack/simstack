from types import SimpleNamespace

import pytest
from odmantic import ObjectId

import simstack.methods.generate_test as generate_test_module
from simstack.models import FloatData, ModelMapping, NamedDataReference


@pytest.mark.asyncio
async def test_load_models_preserves_named_reference_key(monkeypatch):
    model = FloatData(value=4.2)

    class FakeDatabase:
        def __init__(self):
            self.mapping_lookups = 0

        async def find_one(self, model_cls, query):
            if model_cls is FloatData:
                return model
            if model_cls is ModelMapping:
                self.mapping_lookups += 1
                return ModelMapping(
                    name="FloatData",
                    mapping="simstack.models.FloatData",
                    collection_name="float_data",
                )
            return None

    async def fake_import_class(mapping, db):
        return FloatData

    db = FakeDatabase()
    monkeypatch.setattr(generate_test_module, "context", SimpleNamespace(db=db))
    monkeypatch.setattr(generate_test_module, "import_class", fake_import_class)

    reference = NamedDataReference(
        variable_name="curve_sum",
        variable_mapping="simstack.models.FloatData",
        reference=ObjectId(),
    )
    loaded = await generate_test_module.load_models([reference])

    assert loaded == {"curve_sum": model}
    assert db.mapping_lookups == 0


@pytest.mark.asyncio
async def test_load_models_fails_when_reference_is_missing(monkeypatch):
    class FakeDatabase:
        async def find_one(self, model_cls, query):
            return None

    async def fake_import_class(mapping, db):
        return FloatData

    monkeypatch.setattr(
        generate_test_module,
        "context",
        SimpleNamespace(db=FakeDatabase()),
    )
    monkeypatch.setattr(generate_test_module, "import_class", fake_import_class)

    reference = NamedDataReference(
        variable_name="curve_sum",
        variable_mapping="simstack.models.FloatData",
        reference=ObjectId(),
    )

    with pytest.raises(RuntimeError, match="Failed to load"):
        await generate_test_module.load_models([reference])
