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
    value = DomainParent.model_validate_doc({"id": ids[0], "value": 1})
    assert value.id == ids[0]
    assert value.model_dump_doc()["id"] == ids[0]
    schema = DomainParent.model_json_schema()
    assert schema["properties"]["id"]["format"] == "objectid"
    assert "_id" not in schema["properties"]




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
        schema["properties"]["reference"]["x-simstack-reference-model"]
        == "DomainParent"
    )
    assert "x-simstack-reference-collection" not in json.dumps(schema)
    assert "x-simstack-reference" not in schema["properties"]["embedded"]
    assert document["id"] == value.id




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
    a._scientific_revision = "opaque scientific version"
    a._scientific_reference = ("domain_scanner_example.Child", str(a.id))
    assert "_scientific_revision" not in a.__dict__
    assert "_scientific_reference" not in a.__dict__
    assert complex_hash_function(a) == complex_hash_function(b)
    b.scientific_label = "different signal"
    assert complex_hash_function(a) != complex_hash_function(b)


@pytest.mark.asyncio
async def test_model_scanner_registers_indirect_document_inheritance():
    from simstack.tables.model_table import CreateModelTable

    module = types.ModuleType("domain_scanner_example")
    exec(
        "from simstack.models.document import Model\n"
        "class Parent(Model):\n    value: int\n"
        "class Child(Parent):\n    scientific_label: str\n",
        module.__dict__,
    )
    from simstack.tables.catalogue import CatalogueCollector
    catalogue = CatalogueCollector()
    await CreateModelTable(catalogue)._create_models_from_module(module, "")
    entries = list(catalogue.models.values())
    assert {entry.name for entry in entries} == {"Parent", "Child"}
    assert all("collection_name" not in entry.model_dump() for entry in entries)
    assert catalogue.model_types == {
        "domain_scanner_example.Parent": module.Parent,
        "domain_scanner_example.Child": module.Child,
    }


def test_domain_models_do_not_expose_physical_storage_or_query_hooks():
    assert DomainParent.model_fields["id"].alias is None
    identity = ObjectId()
    assert DomainParent.model_validate_doc(
        {"id": identity, "_id": ObjectId(), "value": 1}
    ).id == identity
    assert not hasattr(DomainParent, "__collection__")
    assert not hasattr(DomainParent, "__field_expression_factory__")
    with pytest.raises(AttributeError):
        DomainParent.value
    with pytest.raises(TypeError, match="index"):
        Field(default=0, index=True)


def test_unknown_subtype_is_not_imported_or_accepted():
    with pytest.raises(DocumentParsingError) as error:
        DomainParent.model_validate_doc(
            {"id": ObjectId(), "value": 1, SUBTYPE_KEY: "untrusted.module.Document"}
        )
    assert "incompatible document subtype" in str(error.value.__cause__)
