import importlib
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from simstack.models.document import (
    DocumentParsingError,
    EmbeddedModel,
    Model,
    ObjectId,
    Reference,
    decode_document_value,
    encode_document_value,
)
from simstack.persistence import (
    Comparison,
    CriteriaGroup,
    Junction,
    SortDirection,
    all_of,
    descending,
    eq,
    gte,
    in_values,
)


class EmbeddedValue(EmbeddedModel):
    value: int


class ReferencedValue(Model):
    name: str


class ExampleDocument(Model):
    name: str
    embedded: EmbeddedValue
    reference: ReferencedValue | None = Reference(default=None)


class ExistingNameModel(Model):
    value: int


class BinaryDocument(Model):
    payload: bytes
    nested: dict[str, object]


class IdentityDocument(Model):
    task_id: str
    parent_ids: list[ObjectId]


class DatedDocument(Model):
    timestamp: datetime
    day: date


class ScalarDocument(Model):
    day: date
    identifier: UUID
    amount: Decimal


def test_document_model_is_persistence_neutral_and_json_safe():
    reference_id = ObjectId("0123456789abcdef01234567")
    reference = ReferencedValue(id=reference_id, name="reference")
    value = ExampleDocument(
        name="example",
        embedded=EmbeddedValue(value=7),
        reference=reference,
    )

    assert value.model_dump_doc() == {
        "_id": str(value.id),
        "name": "example",
        "embedded": {"value": 7},
        "reference": str(reference_id),
    }
    assert ExampleDocument.__collection__ == "example_document"


def test_repository_criteria_are_backend_neutral():
    object_id = ObjectId("0123456789abcdef01234567")

    criteria = all_of(
        eq("id", object_id),
        in_values("name", ["first", "second"]),
    )
    sort = descending("name")

    assert isinstance(criteria, CriteriaGroup)
    assert criteria.junction is Junction.ALL
    assert criteria.criteria[0].field == "id"
    assert criteria.criteria[0].comparison is Comparison.EQUAL
    assert criteria.criteria[0].value is object_id
    assert criteria.criteria[1].comparison is Comparison.IN
    assert criteria.criteria[1].value == ("first", "second")
    assert sort.field == "name"
    assert sort.direction is SortDirection.DESCENDING
    assert not hasattr(ExampleDocument, "name")


def test_nullable_references_are_supported():
    value = ExampleDocument(
        name="without-reference",
        embedded=EmbeddedValue(value=1),
    )

    assert value.reference is None
    schema = ExampleDocument.model_json_schema()["properties"]["reference"]
    assert schema["x-simstack-reference"] is True


def test_reference_containers_dump_document_ids_without_embedding_documents():
    class ContainerReferences(Model):
        items: list[ReferencedValue] = Reference(default_factory=list)
        lookup: dict[str, ReferencedValue] = Reference(default_factory=dict)
        pair: tuple[ReferencedValue, ReferencedValue] = Reference()
        variadic: tuple[ReferencedValue, ...] = Reference(default=())

    first, second = ReferencedValue(name="first"), ReferencedValue(name="second")
    value = ContainerReferences(items=[first, second], lookup={"one": first}, pair=(first, second), variadic=(second,))
    document = value.model_dump_doc()

    assert document == {
        "_id": str(value.id),
        "items": [str(first.id), str(second.id)],
        "lookup": {"one": str(first.id)},
        "pair": [str(first.id), str(second.id)],
        "variadic": [str(second.id)],
    }
    assert json.loads(json.dumps(document)) == document
    assert value.items[0] is first
    assert value.lookup["one"] is first
    assert value.pair == (first, second)


@pytest.mark.parametrize("present", [False, True])
def test_optional_reference_containers_preserve_none_and_flatten_present_values(present):
    class NullableReferences(Model):
        items: list[ReferencedValue | None] | None = Reference(default=None)
        lookup: dict[str, ReferencedValue | None] | None = Reference(default=None)
        sequence: tuple[ReferencedValue | None, ...] | None = Reference(default=None)

    referenced = ReferencedValue(name="optional")
    value = NullableReferences(
        items=[referenced, None] if present else None,
        lookup={"present": referenced, "missing": None} if present else None,
        sequence=(referenced, None) if present else None,
    )
    document = value.model_dump_doc()

    assert document["items"] == ([str(referenced.id), None] if present else None)
    assert document["lookup"] == ({"present": str(referenced.id), "missing": None} if present else None)
    assert document["sequence"] == ([str(referenced.id), None] if present else None)


def test_nested_reference_containers_and_alias_dump_ids_at_document_leaves():
    class NestedReferences(Model):
        groups: dict[str, list[tuple[ReferencedValue | None, ...]]] = Reference(alias="referenced_groups")

    referenced = ReferencedValue(name="nested")
    value = NestedReferences(referenced_groups={"group": [(referenced, None), ()], "empty": []})

    assert value.model_dump_doc() == {
        "_id": str(value.id),
        "referenced_groups": {"group": [[str(referenced.id), None], []], "empty": []},
    }


def test_embedded_values_reference_containers_dump_ids_without_changing_embedded_shape():
    class ReferenceEnvelope(EmbeddedModel):
        label: str
        references: list[ReferencedValue] = Reference(default_factory=list)

    class Parent(Model):
        envelopes: list[ReferenceEnvelope]

    referenced = ReferencedValue(name="embedded-reference")
    parent = Parent(envelopes=[ReferenceEnvelope(label="envelope", references=[referenced])])

    assert parent.model_dump_doc() == {
        "_id": str(parent.id),
        "envelopes": [{"label": "envelope", "references": [str(referenced.id)]}],
    }


def test_non_reference_document_fields_and_containers_remain_embedded():
    class EmbeddedDocuments(Model):
        document: ReferencedValue
        items: list[ReferencedValue]
        lookup: dict[str, ReferencedValue]
        sequence: tuple[ReferencedValue, ...]

    referenced = ReferencedValue(name="embedded")
    value = EmbeddedDocuments(document=referenced, items=[referenced], lookup={"one": referenced}, sequence=(referenced,))
    embedded = {"_id": str(referenced.id), "name": "embedded"}

    assert value.model_dump_doc() == {
        "_id": str(value.id),
        "document": embedded,
        "items": [embedded],
        "lookup": {"one": embedded},
        "sequence": [embedded],
    }


def test_reference_container_schema_marks_document_leaves_not_container_nodes():
    class ContainerReferenceSchema(Model):
        items: list[ReferencedValue] = Reference(default_factory=list)
        lookup: dict[str, ReferencedValue] = Reference(default_factory=dict)
        pair: tuple[ReferencedValue, ReferencedValue] = Reference()
        variadic: tuple[ReferencedValue, ...] = Reference(default=())

    properties = ContainerReferenceSchema.model_json_schema()["properties"]
    marker = {
        "x-simstack-reference": True,
        "x-simstack-reference-model": "ReferencedValue",
        "x-simstack-reference-collection": "referenced_value",
    }
    for name in ("items", "lookup", "pair", "variadic"):
        assert not properties[name].get("x-simstack-reference")
        assert "x-simstack-reference-model" not in properties[name]
    leaves = [
        properties["items"]["items"],
        properties["lookup"]["additionalProperties"],
        *properties["pair"]["prefixItems"],
        properties["variadic"]["items"],
    ]
    for leaf in leaves:
        assert {key: leaf.get(key) for key in marker} == marker


def test_nested_nullable_reference_container_schema_keeps_null_branches_unmarked():
    class NestedReferenceSchema(Model):
        groups: dict[str, list[tuple[ReferencedValue | None, ...]]] | None = Reference(default=None)

    field_schema = NestedReferenceSchema.model_json_schema()["properties"]["groups"]
    assert not field_schema.get("x-simstack-reference")
    dictionary, outer_null = field_schema["anyOf"]
    assert dictionary["type"] == "object"
    assert outer_null == {"type": "null"}
    array = dictionary["additionalProperties"]
    variadic = array["items"]
    leaf, inner_null = variadic["items"]["anyOf"]
    assert leaf["x-simstack-reference"] is True
    assert leaf["x-simstack-reference-model"] == "ReferencedValue"
    assert leaf["x-simstack-reference-collection"] == "referenced_value"
    assert inner_null == {"type": "null"}
    for container in (dictionary, array, variadic):
        assert not container.get("x-simstack-reference")
        assert "x-simstack-reference-model" not in container


def test_reference_container_schema_rejects_ambiguous_document_union():
    class AmbiguousReferences(Model):
        items: list[ReferencedValue | ExistingNameModel] = Reference(default_factory=list)

    with pytest.raises(TypeError, match="exactly one document type"):
        AmbiguousReferences.model_json_schema()


def test_heterogeneous_reference_tuple_preserves_individual_document_targets():
    class HeterogeneousReferences(Model):
        pair: tuple[ReferencedValue, ExistingNameModel] = Reference()

    first, second = ReferencedValue(name="first"), ExistingNameModel(value=2)
    value = HeterogeneousReferences(pair=(first, second))

    assert value.model_dump_doc()["pair"] == [str(first.id), str(second.id)]
    field_schema = HeterogeneousReferences.model_json_schema()["properties"]["pair"]
    assert "x-simstack-reference" not in field_schema
    assert [leaf["x-simstack-reference-model"] for leaf in field_schema["prefixItems"]] == [
        "ReferencedValue", "ExistingNameModel"
    ]
    assert [leaf["x-simstack-reference-collection"] for leaf in field_schema["prefixItems"]] == [
        "referenced_value", "existing_name"
    ]


def test_reference_schema_identifies_declared_model_despite_decorative_title():
    class Attachment(Model):
        document: ReferencedValue | None = Reference(default=None, title="Attachment")

    schema = Attachment.model_json_schema()["properties"]["document"]
    assert schema["title"] == "Attachment"
    assert schema["x-simstack-reference-model"] == "ReferencedValue"
    assert schema["x-simstack-reference-collection"] == "referenced_value"


def test_default_collection_name_keeps_existing_storage_name():
    assert ExistingNameModel.__collection__ == "existing_name"


def test_binary_document_round_trip_is_lossless_and_json_safe():
    value = BinaryDocument(
        payload=b"\x00\xffpayload",
        nested={"items": [b"nested", {"empty": b""}]},
    )

    dumped = value.model_dump_doc()

    assert dumped["payload"] == {
        "$binary": {"base64": "AP9wYXlsb2Fk", "subType": "00"}
    }
    assert json.loads(json.dumps(dumped)) == dumped
    restored = BinaryDocument.model_validate_doc(dumped)
    assert restored.payload == value.payload
    assert restored.nested == value.nested


def test_document_common_typed_scalars_round_trip_without_loss():
    original = ScalarDocument(day=date(2026, 10, 7), identifier=UUID("01234567-89ab-cdef-0123-456789abcdef"), amount=Decimal("1234567890.0123456789"))
    dumped = original.model_dump_doc()
    assert dumped["identifier"] == str(original.identifier)
    assert dumped["amount"] == "1234567890.0123456789"
    assert json.loads(json.dumps(dumped)) == dumped
    assert ScalarDocument.model_validate_doc(dumped) == original


def test_public_document_value_codec_handles_nested_binary_values():
    original = {"data": [b"one", {"two": bytearray(b"two")}]}

    encoded = encode_document_value(original)

    assert decode_document_value(encoded) == {
        "data": [b"one", {"two": b"two"}]
    }


@pytest.mark.parametrize(
    "binary",
    [
        {"base64": "b25l"},
        {"base64": "b25l", "subType": "00", "unexpected": True},
    ],
)
def test_binary_document_wire_shape_is_strict(binary):
    with pytest.raises(DocumentParsingError):
        decode_document_value({"$binary": binary})


def test_binary_document_envelope_rejects_sibling_keys():
    with pytest.raises(DocumentParsingError):
        decode_document_value(
            {
                "$binary": {"base64": "b25l", "subType": "00"},
                "unexpected": True,
            }
        )


def test_filestack_future_annotations_do_not_corrupt_string_fields():
    from simstack.models.files import FileStack

    file_stack = FileStack(name="result.dat", hash="sha256")
    name_schema = FileStack.model_json_schema()["properties"]["name"]

    assert file_stack.name == "result.dat"
    assert file_stack.hash == "sha256"
    assert {entry.get("type") for entry in name_schema["anyOf"]} == {
        "string",
        "null",
    }


def test_filestack_ui_schema_uses_the_runtime_model_mapping():
    from simstack.models.files import FileStack

    class CustomFileStack(FileStack):
        pass

    assert CustomFileStack.ui_base_schema()["ui:options"]["model"] == (
        f"{CustomFileStack.__module__}.{CustomFileStack.__name__}"
    )


def test_deleted_filestack_keeps_identity_but_cannot_materialize(tmp_path):
    from simstack.models.files import FileStack
    from simstack.models.parameters import Resource

    file_stack = FileStack(
        name="result.dat",
        size=6,
        in_memory=True,
        content=b"stale bytes",
        deleted_at=datetime.now(timezone.utc),
    )
    restored = FileStack.model_validate(file_stack.model_dump())

    assert restored.id == file_stack.id
    assert restored.deleted_at == file_stack.deleted_at
    with pytest.raises(ValueError, match="content has been deleted"):
        restored.get(tmp_path)
    with pytest.raises(ValueError, match="content has been deleted"):
        restored.get_raw(Resource(value="local"), tmp_path)
    with pytest.raises(ValueError, match="content has been deleted"):
        restored.materialize_inline(tmp_path)
    assert not list(tmp_path.iterdir())


def test_object_id_type_is_preserved_in_python_dump_only():
    parent_id = ObjectId("0123456789abcdef01234567")
    value = IdentityDocument(
        task_id="fedcba987654321001234567",
        parent_ids=[parent_id],
    )

    python_dump = value.model_dump(by_alias=True)
    json_dump = value.model_dump(mode="json", by_alias=True)

    assert isinstance(python_dump["id"], ObjectId)
    assert isinstance(python_dump["parent_ids"][0], ObjectId)
    assert type(python_dump["task_id"]) is str
    assert type(json_dump["id"]) is str
    assert type(json_dump["parent_ids"][0]) is str
    assert type(json_dump["task_id"]) is str


@pytest.mark.parametrize("schema_mode", ["validation", "serialization"])
def test_document_public_identity_schema_is_id_and_storage_identity_remains_id_alias(schema_mode):
    value = ReferencedValue(name="identity")
    assert value.model_dump(mode="json", by_alias=True) == {"id": str(value.id), "name": "identity"}
    assert value.model_dump_doc() == {"_id": str(value.id), "name": "identity"}
    assert "id" in ReferencedValue.model_json_schema(mode=schema_mode)["properties"]
    assert "_id" not in ReferencedValue.model_json_schema(mode=schema_mode)["properties"]
    assert ReferencedValue.model_validate_doc(value.model_dump_doc()).id == value.id


def test_generated_object_ids_are_bson_compatible_and_monotonic():
    before = int(time.time())
    object_ids = [ObjectId() for _ in range(3)]
    after = int(time.time())

    assert all(ObjectId.is_valid(value) for value in object_ids)
    assert object_ids == sorted(object_ids)
    assert len(set(object_ids)) == len(object_ids)
    assert all(before <= int(value[:8], 16) <= after for value in object_ids)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Unix fork semantics")
def test_forked_identifier_generator_has_independent_namespace_and_unlocked_counter():
    script = """
import os
from simstack.models.document import ObjectId
from simstack.models.document import identity
parent = ObjectId()
identity._OBJECT_ID_COUNTER_LOCK.acquire()
pid = os.fork()
if pid == 0:
    print(ObjectId(), flush=True)
    os._exit(0)
identity._OBJECT_ID_COUNTER_LOCK.release()
os.waitpid(pid, 0)
print(parent, flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    child_id, parent_id = result.stdout.splitlines()
    assert ObjectId.is_valid(child_id) and ObjectId.is_valid(parent_id)
    assert child_id[8:18] != parent_id[8:18]


@pytest.mark.parametrize(
    ("list_model", "target_name", "collection_name"),
    [
        ("simstack.models.file_list.FileList", "FileStack", "file_stack"),
        ("simstack.models.file_list.FileListModel", "FileStack", "file_stack"),
        ("simstack.models.array_list.ArrayList", "ArrayStorage", "array_storage"),
        ("simstack.models.base_lists.StringDataList", "StringData", "string_data"),
        (
            "simstack.models.base_lists.BooleanDataList",
            "BooleanData",
            "boolean_data",
        ),
    ],
)
def test_object_list_schema_identifies_referenced_model(
    list_model,
    target_name,
    collection_name,
):
    module_name, class_name = list_model.rsplit(".", 1)
    model_class = getattr(importlib.import_module(module_name), class_name)
    items = model_class.model_json_schema()["properties"]["elements"]["items"]

    assert items == {
        "type": "string",
        "x-simstack-reference": True,
        "x-simstack-reference-model": target_name,
        "x-simstack-reference-collection": collection_name,
        "title": f"{target_name} reference",
    }


def test_date_wire_values_round_trip_and_criteria_preserve_domain_value():
    timestamp = datetime(2026, 8, 14, 14, 30, 15, tzinfo=timezone.utc)
    day = date(2026, 8, 14)
    value = DatedDocument(timestamp=timestamp, day=day)

    dumped = value.model_dump_doc()
    criterion = gte("timestamp", timestamp)

    assert dumped["timestamp"] == {"$date": "2026-08-14T14:30:15+00:00"}
    assert dumped["day"] == {"$date": "2026-08-14"}
    assert criterion.field == "timestamp"
    assert criterion.comparison is Comparison.GREATER_THAN_OR_EQUAL
    assert criterion.value == timestamp
    restored = DatedDocument.model_validate_doc(dumped)
    assert restored.timestamp == timestamp
    assert restored.day == day
