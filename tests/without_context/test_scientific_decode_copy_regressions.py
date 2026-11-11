"""Scientific decoding and tuple copying preserve locally materialized values."""

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, Field

from simstack.models import FloatData, IntData
from simstack.models.document import Model, ObjectId
from simstack.models.datasettuple import DataSetTupleSection
from simstack.server_api.scientific_value_graph import decode_graph, encode_graph, type_name
from simstack.server_api.scientific_values import ScientificValuesAPI
from simstack.util.model_copy import model_copy


class PlainEmbeddedScientificValue(BaseModel):
    amount: int


class PlainEmbeddedScientificHolder(Model):
    details: PlainEmbeddedScientificValue


@pytest.mark.asyncio
async def test_decode_retains_catalogue_imported_plain_basemodel(monkeypatch):
    from simstack.models.document.models import _MODEL_TYPES
    from simstack.util import importer

    class DynamicHolder(Model):
        details: Any

    source = DynamicHolder(details=PlainEmbeddedScientificValue(amount=7))
    mapping = type_name(PlainEmbeddedScientificValue)
    assert mapping not in _MODEL_TYPES
    imported = []

    async def import_class(requested, api):
        imported.append(requested)
        assert requested == mapping
        return PlainEmbeddedScientificValue

    monkeypatch.setattr(importer, "import_class", import_class)
    restored = (await ScientificValuesAPI(SimpleNamespace())._decode_values(encode_graph([source])))[0]
    assert type(restored.details) is PlainEmbeddedScientificValue
    assert restored.details.amount == 7
    assert imported == [mapping]
    assert mapping not in _MODEL_TYPES


@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize("preserve_ids", [False, True])
@pytest.mark.parametrize("copy_references", [False, True])
def test_tuple_copy_aligns_cloned_ids_and_preserves_sharing(deep, preserve_ids, copy_references):
    row = IntData(value=1)
    source = DataSetTupleSection()
    source.extend([(row, row), (row, row)])
    object.__setattr__(source, "_shared_ids", source.data[0])
    copied = model_copy(source, deep=deep, preserve_ids=preserve_ids, copy_references=copy_references)
    copied_row = copied[0][0]
    cloned = deep and copy_references
    assert (copied_row is not row) == cloned
    assert (copied_row.id != row.id) == (cloned and not preserve_ids)
    assert copied[0][1] is copied[1][0] is copied[1][1] is copied_row
    assert copied.data == [[copied_row.id, copied_row.id]] * 2
    assert copied._shared_ids is copied.data[0]
    assert (copied.data is not source.data) == deep
    assert source.data == [[row.id, row.id]] * 2
    if cloned:
        copied_row.value = 99
        assert row.value == 1


@pytest.mark.parametrize("cached", [False, True])
def test_tuple_copy_preserves_explicit_unmaterialized_or_replaced_ids(cached):
    row = IntData(value=1)
    explicit = ObjectId()
    source = DataSetTupleSection(model_types=["IntData"], data=[[row.id], [explicit]])
    if cached:
        object.__setattr__(source, "_cache", [(row,)])
    copied = model_copy(source, copy_references=True)
    assert copied.data[1] == [explicit]
    assert copied.data[0] == [copied._cache[0][0].id if cached else row.id]
    assert source.data == [[row.id], [explicit]]

    if cached:
        source.data[0] = [explicit]
        copied = model_copy(source, copy_references=True)
        assert copied.data == [[explicit], [explicit]]


def test_tuple_copy_qualifies_identical_record_ids_by_model_column():
    identity = ObjectId()
    source = DataSetTupleSection()
    source.extend([(IntData(id=identity, value=1), FloatData(id=identity, value=2.5))])
    copied = model_copy(source, copy_references=True)
    assert copied[0][0].id != copied[0][1].id
    assert copied.data[0] == [model.id for model in copied[0]]
    assert source.data == [[identity, identity]]


def test_plain_basemodel_decoding_uses_names_after_resolving_wire_aliases():
    class AliasedValue(BaseModel):
        x: int = Field(alias="y")
        y: int = Field(alias="z")
        amount: int = Field(validation_alias=AliasChoices("input_amount", AliasPath("input", 0)),
                            serialization_alias="energy")

    class Holder(Model):
        detail: AliasedValue

    original = Holder(detail=AliasedValue(y=5, z=7, input=[11]))
    classes = {type_name(Holder): Holder, type_name(AliasedValue): AliasedValue}
    decoded = decode_graph(encode_graph([original]), classes.__getitem__)[0]
    assert type(decoded.detail) is AliasedValue
    assert decoded.detail.x == 5 and decoded.detail.y == 7 and decoded.detail.amount == 11
