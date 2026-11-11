"""Typed extras obey the same scientific graph contract as declared fields."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, Field

from simstack.core.node import compute_arg_hash
from simstack.models import IntData
from simstack.models.document import Model, Reference
from simstack.server_api.scientific_value_graph import (
    bind_publication_revisions,
    encode_graph,
    type_name,
)
from simstack.server_api.scientific_values import ScientificValuesAPI
from simstack.util.model_copy import model_copy


class Reading(BaseModel):
    value: int = Field(validation_alias="input", serialization_alias="amount")


class CelsiusReading(Reading):
    pass


class KelvinReading(Reading):
    pass


class ExtraReadings(Model):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, list[Reading | None]] = Field(init=False)


class InheritedExtraReadings(ExtraReadings):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [ExtraReadings, InheritedExtraReadings])
async def test_nested_extra_subtype_aliases_copy_and_hash_survive_without_catalogue(
    cls, monkeypatch
):
    async def unexpected_import(*args):
        pytest.fail("A declared extra type must not require its own catalogue row")

    monkeypatch.setattr("simstack.util.importer.import_class", unexpected_import)
    source = cls(samples=[CelsiusReading(input=20), None])
    copied = model_copy(source)
    assert copied.samples[0] is not source.samples[0]
    assert type(copied.samples[0]) is CelsiusReading
    assert copied.id != source.id
    (restored,) = await ScientificValuesAPI(SimpleNamespace())._decode_values(
        encode_graph([copied])
    )
    assert type(restored.samples[0]) is CelsiusReading
    assert restored.samples[0].value == 20
    assert restored.samples[1] is None
    assert (
        compute_arg_hash([source])
        == compute_arg_hash([copied])
        == compute_arg_hash([restored])
    )
    assert compute_arg_hash([source]) != compute_arg_hash(
        [cls(samples=[KelvinReading(input=20), None])]
    )
    assert compute_arg_hash([source]) != compute_arg_hash(
        [cls(samples=[CelsiusReading(input=21), None])]
    )


class ReferencedReading(BaseModel):
    value: IntData = Reference()


class ExtraReferences(Model):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, list[ReferencedReading]] = Field(init=False)


def test_references_nested_inside_extras_are_published_and_revision_bound():
    row = IntData(value=7)
    source = ExtraReferences(rows=[ReferencedReading(value=row)])
    graph = encode_graph([source])
    assert {node["mapping"] for node in graph["nodes"]} == {
        type_name(ExtraReferences),
        type_name(IntData),
    }
    for node in graph["nodes"]:
        node["submitted_revision"] = "extra-revision"
    bind_publication_revisions([source], graph)
    assert row._scientific_revision == "extra-revision"
    assert source._scientific_revision == "extra-revision"
    assert all(
        node["revision"] == "extra-revision" for node in encode_graph([source])["nodes"]
    )
