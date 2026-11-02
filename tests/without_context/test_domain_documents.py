"""Domain contracts independent of a database, engine, or driver installation."""

import json
import math
import types
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import pytest
from pydantic import field_serializer

from simstack.core.hash import complex_hash_function
from simstack.models.document import (
    DocumentParsingError,
    EmbeddedModel,
    Field,
    Model,
    ObjectId,
    Reference,
    SUBTYPE_KEY,
    decode_document_value,
    encode_document_value,
)
from simstack.persistence.criteria import normalize_criteria
from simstack.util.model_copy import model_copy


class DomainParent(Model):
    value: int


class DomainChild(DomainParent):
    scientific_label: str


class DomainValue(EmbeddedModel):
    label: str


class DomainSpecialValue(DomainValue):
    measurements: list[float]


class DomainGraph(Model):
    reference: DomainParent = Reference()
    embedded: DomainValue = Reference()
    references: Optional[list[DomainParent]] = Reference(default=None)
    child_map: dict[str, DomainParent] = Field(default_factory=dict)


def test_ids_are_validated_neutral_strings_with_server_schema_metadata():
    ids = [ObjectId() for _ in range(4)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 4
    assert all(isinstance(value, str) and len(value) == 24 for value in ids)
    assert ObjectId(str(ids[0]).upper()) == ids[0]
    assert ObjectId(bytes.fromhex(ids[0])) == ids[0]
    assert ids[0].binary == bytes.fromhex(ids[0])
    known = ObjectId("000000010000000000000000")
    assert known.generation_time == datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    assert not ObjectId.is_valid(None)
    with pytest.raises(ValueError):
        ObjectId("not an identity")
    value = DomainParent.model_validate_doc({"_id": ids[0], "value": 1})
    assert value.id == ids[0]
    assert value.model_dump_doc()["id"] == ids[0]
    schema = DomainParent.model_json_schema()
    assert schema["properties"]["id"]["format"] == "objectid"
    assert "_id" not in schema["properties"]


def test_class_fields_are_neutral_criteria_and_inherited_fields_work():
    criteria = (DomainChild.value >= 2) & DomainChild.scientific_label.in_(["a", "b"])
    assert normalize_criteria(criteria) == {
        "op": "and",
        "items": [
            {"op": "gte", "field": "value", "value": 2},
            {"op": "in", "field": "scientific_label", "value": ["a", "b"]},
        ],
    }
    assert DomainChild.value.desc().to_dict() == {"field": "value", "direction": -1}
    assert DomainGraph.embedded.label.path == "embedded.label"
    with pytest.raises(ValueError, match="neutral criteria"):
        normalize_criteria({"value": {"$gte": 2}})
    with pytest.raises(TypeError, match="Combine criteria"):
        bool(DomainParent.value == 1)


def test_references_embedded_values_subtypes_and_containers_keep_identity():
    child = DomainChild(value=1, scientific_label="spectrum")
    value = DomainGraph(
        reference=child,
        embedded=DomainSpecialValue(label="series", measurements=[1.5]),
        references=[child],
        child_map={"input": child},
    )
    document = value.model_dump_doc()
    assert document["reference"]["id"] == child.id
    assert document["reference"][SUBTYPE_KEY].endswith("DomainChild")
    assert document["references"] == [document["reference"]]
    assert document["embedded"]["measurements"] == [1.5]
    assert document["child_map"]["input"]["scientific_label"] == "spectrum"
    # A flat reference is hydrated by the repository. Embedded values can be
    # reconstructed directly, and must retain concrete subclass-only fields.
    hydrated = {
        **document,
        "reference": document["child_map"]["input"],
        "references": [document["child_map"]["input"]],
    }
    restored = DomainGraph.model_validate_doc(hydrated)
    assert isinstance(restored.reference, DomainChild)
    assert isinstance(restored.embedded, DomainSpecialValue)
    assert isinstance(restored.child_map["input"], DomainChild)
    assert restored.reference.scientific_label == "spectrum"
    schema = DomainGraph.model_json_schema()
    assert (
        schema["properties"]["reference"]["x-simstack-reference-collection"]
        == "domain_parent"
    )
    assert "x-simstack-reference" not in schema["properties"]["embedded"]
    assert document["id"] == value.id


def test_custom_primary_key_alias_metadata_and_serializers_stay_explicit():
    class Measurement(Model):
        model_config = {"collection": "measurements"}
        key: str = Field(primary_field=True)
        name: str = Field(key_name="display_name", unique=True)
        value: int

        @field_serializer("value")
        def value_serializer(self, value):
            return value + 1

    value = Measurement(key="run-A", name="signal", value=2)
    assert value.id == "run-A"
    assert value.model_dump_doc() == {
        "id": "run-A",
        "display_name": "signal",
        "value": 3,
    }
    assert Measurement.__collection__ == "measurements"
    assert (Measurement.key == "run-A").to_dict() == {
        "op": "eq",
        "field": "id",
        "value": "run-A",
    }
    assert (Measurement.id == "run-A").to_dict() == {
        "op": "eq",
        "field": "id",
        "value": "run-A",
    }
    assert (
        Measurement.model_fields["name"].json_schema_extra["x-simstack-index"]["unique"]
        is True
    )
    assert Measurement.name.path == "display_name"
    schema = Measurement.model_json_schema()
    assert schema["properties"]["id"]["type"] == "string"
    assert "format" not in schema["properties"]["id"]
    assert "key" not in schema["properties"]
    assert schema["x-simstack-primary-field"] == "key"
    restored = Measurement.model_validate_doc(
        {"id": "run-A", "display_name": "signal", "value": 2}
    )
    assert restored.key == "run-A"
    assert model_copy(value, preserve_ids=True).id == "run-A"
    with pytest.raises(ValueError, match="preserve_ids"):
        model_copy(value)


def test_wire_codec_retains_binary_numeric_datetime_and_reserved_user_mapping():
    scientific = {
        "binary": b"\x00\xff\x81",
        "large": 2**63 - 1,
        "nan": math.nan,
        "positive_infinity": math.inf,
        "negative_infinity": -math.inf,
        "decimal": Decimal("1.00000000000000000001"),
        "when": datetime(2026, 1, 2, 3, tzinfo=timezone.utc),
        "day": date(2026, 1, 2),
        "metadata": {"__simstack_type__": "date", "value": "literal user data"},
    }
    wire = encode_document_value(scientific)
    json.dumps(wire, allow_nan=False)
    restored = decode_document_value(wire)
    assert restored["binary"] == scientific["binary"]
    assert restored["large"] == scientific["large"]
    assert math.isnan(restored["nan"])
    assert restored["positive_infinity"] == math.inf
    assert restored["negative_infinity"] == -math.inf
    for key in ["decimal", "when", "day", "metadata"]:
        assert restored[key] == scientific[key]
    with pytest.raises(DocumentParsingError):
        decode_document_value({"__simstack_type__": "bytes", "value": "invalid base64"})


def test_model_json_and_document_binary_round_trip():
    class BinaryScience(Model):
        payload: bytes
        metadata: dict

    value = BinaryScience(payload=b"\xff\x00", metadata={"tag": "value"})
    assert (
        BinaryScience.model_validate_json(value.model_dump_json()).payload
        == value.payload
    )
    assert (
        BinaryScience.model_validate_doc(value.model_dump_doc()).payload
        == value.payload
    )


def test_document_include_exclude_does_not_reintroduce_nested_values():
    class Container(Model):
        values: list[DomainChild]
        labels: dict[str, DomainChild]

    first = DomainChild(value=1, scientific_label="first")
    second = DomainChild(value=2, scientific_label="second")
    value = Container(values=[first, second], labels={"a": first, "b": second})
    document = value.model_dump_doc(
        include={"values": {0: {"value"}}, "labels": {"a": {"value"}}}
    )
    assert document == {"values": [{"value": 1}], "labels": {"a": {"value": 1}}}
    document = value.model_dump_doc(
        exclude={"values": {"__all__": {"scientific_label"}}, "labels": {"b"}}
    )
    assert "scientific_label" not in document["values"][0]
    assert "scientific_label" not in document["values"][1]
    assert set(document["labels"]) == {"a"}


def test_inherited_scientific_hash_ignores_ids_and_repository_private_state():
    a = DomainChild(value=1, scientific_label="signal")
    b = model_copy(a)
    assert a.id != b.id
    assert complex_hash_function(a) == complex_hash_function(b)
    object.__setattr__(a, "_simstack_persisted_document", a.model_dump_doc())
    object.__setattr__(a, "_simstack_refreshed_fields", {"value"})
    assert complex_hash_function(a) == complex_hash_function(b)
    b.scientific_label = "different signal"
    assert complex_hash_function(a) != complex_hash_function(b)


@pytest.mark.asyncio
async def test_model_scanner_registers_indirect_document_inheritance():
    from simstack.persistence.memory import InMemoryRepository
    from simstack.tables.model_table import CreateModelTable
    from simstack.models.models import ModelMapping

    module = types.ModuleType("domain_scanner_example")
    exec(
        "from simstack.models.document import Model\n"
        "class Parent(Model):\n    value: int\n"
        "class Child(Parent):\n    scientific_label: str\n",
        module.__dict__,
    )
    store = InMemoryRepository()
    await CreateModelTable(store)._create_models_from_module(module, "")
    entries = await store.find(ModelMapping)
    assert {entry.name for entry in entries} == {"Parent", "Child"}
    assert {entry.collection_name for entry in entries} == {"parent", "child"}


def test_unknown_subtype_is_not_imported_or_accepted():
    with pytest.raises(DocumentParsingError) as error:
        DomainParent.model_validate_doc(
            {"id": ObjectId(), "value": 1, SUBTYPE_KEY: "untrusted.module.Document"}
        )
    assert "incompatible document subtype" in str(error.value.__cause__)


@pytest.mark.asyncio
async def test_explicit_collection_inheritance_retains_type_when_loaded_as_parent():
    from simstack.persistence.memory import InMemoryRepository

    class SharedParent(Model):
        model_config = {"collection": "shared_scientific_records"}
        value: int

    class SharedChild(SharedParent):
        additional_value: int

    assert SharedChild.__collection__ == SharedParent.__collection__
    store = InMemoryRepository()
    original = SharedChild(value=1, additional_value=2)
    await store.save(original)
    restored = await store.find_one(SharedParent, SharedParent.id == original.id)
    assert isinstance(restored, SharedChild)
    assert restored.additional_value == 2
    assert restored.id == original.id


@pytest.mark.asyncio
async def test_custom_integer_primary_reference_keeps_its_native_identity():
    from simstack.persistence.memory import InMemoryRepository

    class IntegerIdentity(Model):
        key: int = Field(primary_field=True)
        label: str

    class IntegerReference(Model):
        item: IntegerIdentity = Reference()

    store = InMemoryRepository()
    original = IntegerReference(item=IntegerIdentity(key=42, label="sample"))
    assert original.model_dump_doc()["item"] == 42
    assert (IntegerIdentity.id == 42).to_dict() == {
        "op": "eq",
        "field": "id",
        "value": 42,
    }
    await store.save(original)
    restored = await store.find_one(
        IntegerReference, IntegerReference.id == original.id
    )
    assert restored.item.id == 42
    assert restored.item.key == 42
    assert restored.item.label == "sample"
