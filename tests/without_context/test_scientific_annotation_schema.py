"""Declared embedded values and wire schemas need no accidental alias matches."""

from types import SimpleNamespace
from typing import Annotated
from decimal import Decimal

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, Field, computed_field, field_validator

from simstack.models.document import Model, Field as DocumentField, Reference
from simstack.models.document.codec import encode_document_value
from simstack.server_api.scientific_value_graph import decode_graph, encode_graph, type_name
from simstack.server_api.scientific_values import ScientificValuesAPI


class PlainTree(BaseModel):
    value: int
    children: list["PlainTree"] = []


class DetailedTree(PlainTree):
    label: str


class AnnotatedHolder(Model):
    trees: dict[str, list[Annotated[PlainTree | None, "science"]]]
    pair: tuple[PlainTree, ...]


@pytest.mark.asyncio
async def test_declared_plain_containers_recursion_and_loaded_subtypes_need_no_catalogue(monkeypatch):
    from simstack.util import importer

    async def unexpected_import(*args):
        raise AssertionError("Declared types should not need independent catalogue entries")

    monkeypatch.setattr(importer, "import_class", unexpected_import)
    leaf = DetailedTree(value=2, label="leaf")
    source = AnnotatedHolder(trees={"run": [PlainTree(value=1, children=[leaf]), None]}, pair=(leaf,))
    restored = (await ScientificValuesAPI(SimpleNamespace())._decode_values(encode_graph([source])))[0]
    assert restored.model_dump() == source.model_dump()
    assert type(restored.trees["run"][0].children[0]) is DetailedTree
    assert type(restored.pair[0]) is DetailedTree


@pytest.mark.asyncio
async def test_unknown_embedded_wire_mapping_still_requires_catalogue(monkeypatch):
    from simstack.util import importer

    source = AnnotatedHolder(trees={}, pair=(PlainTree(value=1),))
    graph = encode_graph([source])
    graph["nodes"][0]["value"]["pair"]["items"][0]["mapping"] = "untrusted.module.Value"
    calls = []

    async def denied(mapping, _api):
        calls.append(mapping)
        raise LookupError("not in revision catalogue")

    monkeypatch.setattr(importer, "import_class", denied)
    with pytest.raises(LookupError, match="not in revision catalogue"):
        await ScientificValuesAPI(SimpleNamespace())._decode_values(graph)
    assert calls == ["untrusted.module.Value"]


class AliasedWireModel(Model):
    first: int = Field(alias="a", serialization_alias="b")
    second: str = Field(alias="b", serialization_alias="c")
    amount: float = Field(validation_alias=AliasChoices(AliasPath("input", 0), "input_amount"), serialization_alias="energy")
    path_only: int = Field(validation_alias=AliasPath("path", 0), serialization_alias="path_energy")


class SwappedAliases(Model):
    left: int = Field(alias="right", serialization_alias="right")
    right: str = Field(alias="left", serialization_alias="left")


class KeyedRecord(Model):
    key: str = DocumentField(primary_field=True, validation_alias=AliasChoices("input_key", "key"), serialization_alias="record_key")
    child: AliasedWireModel = Reference()


@pytest.mark.parametrize("mode", ["validation", "serialization"])
@pytest.mark.parametrize("by_alias", [False, True])
def test_wire_schema_handles_alias_chains_choices_paths_and_swaps(mode, by_alias):
    schema = AliasedWireModel.model_json_schema(mode=mode, by_alias=by_alias)
    assert set(schema["properties"]) == {"id", "b", "c", "energy", "path_energy"}
    assert schema["properties"]["b"]["type"] == "integer"
    assert schema["properties"]["c"]["type"] == "string"
    assert schema["properties"]["energy"]["type"] == "number"
    assert set(schema["required"]) == {"b", "c", "energy", "path_energy"}
    swapped = SwappedAliases.model_json_schema(mode=mode, by_alias=by_alias)
    assert swapped["properties"]["right"]["type"] == "integer"
    assert swapped["properties"]["left"]["type"] == "string"
    keyed = KeyedRecord.model_json_schema(mode=mode, by_alias=by_alias)
    assert keyed["properties"]["id"]["type"] == "string"
    assert "record_key" not in keyed["properties"]
    assert keyed["properties"]["child"]["x-simstack-reference"] is True
    assert set(keyed["required"]) == {"id", "child"}


class PlainAliasedTree(BaseModel):
    id: int = Field(validation_alias="input_id", serialization_alias="scientific_id")
    first: int = Field(alias="left", serialization_alias="right")
    second: str = Field(alias="right", serialization_alias="left")
    decimal: Decimal
    children: list["PlainAliasedTree"] = []

    @computed_field
    @property
    def total(self) -> int:
        return self.first + len(self.children)


class PlainAlternative(BaseModel):
    value: int = Field(validation_alias=AliasPath("input", 0), serialization_alias="energy")


class PlainSchemaContainers(Model):
    nested: dict[str, list[PlainAliasedTree | None]]
    fixed: tuple[PlainAlternative, int]
    repeated: tuple[PlainAlternative, ...]
    either: PlainAlternative | PlainAliasedTree


class SecondPlainSchemaHolder(Model):
    detail: PlainAliasedTree


class SharedDefinitionsOuter(BaseModel):
    first: PlainSchemaContainers
    second: SecondPlainSchemaHolder


@pytest.mark.parametrize("mode", ["validation", "serialization"])
@pytest.mark.parametrize("by_alias", [False, True])
def test_nested_plain_alias_definitions_are_normalized_once_without_changing_schema_mode(mode, by_alias):
    schema = SharedDefinitionsOuter.model_json_schema(mode=mode, by_alias=by_alias)
    tree = schema["$defs"]["PlainAliasedTree"]
    assert tree["properties"]["scientific_id"]["type"] == "integer"
    assert "id" not in tree["properties"]
    assert tree["properties"]["right"]["type"] == "integer"
    assert tree["properties"]["left"]["type"] == "string"
    assert tree["properties"]["children"]["items"]["$ref"] == "#/$defs/PlainAliasedTree"
    assert ("total" in tree["properties"]) == (mode == "serialization")
    if mode == "validation":
        assert "anyOf" in tree["properties"]["decimal"]
    else:
        assert tree["properties"]["decimal"]["type"] == "string"
    alternative = schema["$defs"]["PlainAlternative"]
    assert set(alternative["properties"]) == {"energy"}
    assert alternative["properties"]["energy"]["type"] == "integer"
    assert alternative["required"] == ["energy"]


@pytest.mark.parametrize("override,mode", [("serialization", "validation"), ("validation", "serialization")])
def test_nested_plain_schema_respects_its_explicit_mode_override(override, mode):
    class Value(BaseModel):
        model_config = {"json_schema_mode_override": override}
        first: int = Field(alias="a", serialization_alias="b")
        second: str = Field(alias="b", serialization_alias="c")

    class Holder(Model):
        detail: Value

    schema = Holder.model_json_schema(mode=mode)
    plain = schema["$defs"]["Value"]
    assert plain["properties"]["b"]["type"] == "integer"
    assert plain["properties"]["c"]["type"] == "string"
    assert set(plain["required"]) == {"b", "c"}


@pytest.mark.parametrize("null_first", [False, True])
def test_nested_plain_nullable_schema_handles_pydantic_null_branch_reordering(null_first):
    class Value(BaseModel):
        amount: int = Field(validation_alias="input_amount", serialization_alias="energy")

    nullable = type(None) | Value if null_first else Value | None

    class Holder(Model):
        detail: nullable

    schema = Holder.model_json_schema()
    plain = schema["$defs"]["Value"]
    assert set(plain["properties"]) == {"energy"}
    assert plain["required"] == ["energy"]
    assert {"type": "null"} in schema["properties"]["detail"]["anyOf"]


@pytest.mark.parametrize("aliased", [False, True])
def test_untagged_gui_values_preserve_before_validator_order_and_decode_markers_only_once(aliased):
    events = []

    class Value(BaseModel):
        amount: int = Field(validation_alias="input_amount", serialization_alias="energy") if aliased else Field()
        metadata: dict

        @field_validator("amount", mode="before")
        @classmethod
        def child_validator(cls, value):
            events.append("child")
            return value

        @field_validator("amount", mode="after")
        @classmethod
        def child_after_validator(cls, value):
            events.append("child after")
            return value

    class Holder(Model):
        detail: Value

        @field_validator("detail", mode="before")
        @classmethod
        def parent_validator(cls, value):
            assert isinstance(value, dict)
            events.append("parent")
            key = "input_amount" if aliased else "amount"
            return {**value, key: value[key] + 1}

        @field_validator("detail", mode="after")
        @classmethod
        def parent_after_validator(cls, value):
            events.append("parent after")
            return value

    from simstack.models.document import ObjectId
    identity = str(ObjectId())
    metadata = {"__simstack_type__": "bytes", "value": "not a binary envelope"}
    mapping = type_name(Holder)
    graph = {"roots": [{"mapping": mapping, "id": identity}], "nodes": [{
        "mapping": mapping, "id": identity,
        "value": {"id": identity, "detail": {"energy" if aliased else "amount": 7, "metadata": encode_document_value(metadata)}},
    }]}
    restored = decode_graph(graph, {mapping: Holder}.__getitem__)[0]
    assert restored.detail.amount == 8
    assert restored.detail.metadata == metadata
    assert events == ["parent", "child", "child after", "parent after"]


def test_untagged_gui_alias_choices_and_paths_share_constructor_containers():
    class Value(BaseModel):
        amount: int = Field(validation_alias=AliasChoices(AliasPath("inputs", 0), "input_amount"), serialization_alias="energy")
        description: str = Field(validation_alias=AliasPath("inputs", 1), serialization_alias="text")
        last: int = Field(validation_alias=AliasPath("tail", -1), serialization_alias="last_value")
        first: int = Field(validation_alias="left", serialization_alias="right")
        second: str = Field(validation_alias="right", serialization_alias="left")

    class Holder(Model):
        details: dict[str, list[Value | None]]

    from simstack.models.document import ObjectId
    identity = str(ObjectId())
    mapping = type_name(Holder)
    graph = {"roots": [{"mapping": mapping, "id": identity}], "nodes": [{
        "mapping": mapping, "id": identity,
        "value": {"id": identity, "details": {"one": [None, {
            "energy": 7, "text": "sample", "last_value": 9, "right": 4, "left": "other",
        }]}},
    }]}
    restored = decode_graph(graph, {mapping: Holder}.__getitem__)[0]
    assert restored.details["one"][0] is None
    assert restored.details["one"][1].model_dump() == {
        "amount": 7, "description": "sample", "last": 9, "first": 4, "second": "other",
    }


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("kind", ["indices", "shared_choices", "preferred_swap"])
def test_untagged_gui_compatible_validation_paths_preserve_both_fields(kind, reverse):
    from pydantic import create_model
    from simstack.models.document import ObjectId

    aliases = {
        "indices": (AliasPath("inputs", -1), AliasPath("inputs", 0)),
        "shared_choices": (AliasChoices("shared", "input_first"), AliasChoices("shared", "input_second")),
        "preferred_swap": (AliasChoices("input_first", "input_second"), AliasChoices("input_second", "input_first")),
    }[kind]
    fields = [("first", (int, Field(validation_alias=aliases[0], serialization_alias="first_wire"))),
              ("second", (int, Field(validation_alias=aliases[1], serialization_alias="second_wire")))]
    if reverse:
        fields.reverse()
    Value = create_model("CompatiblePaths", **dict(fields))
    ordinary = Value(**({"inputs": [2, 1]} if kind == "indices" else {"input_first": 1, "input_second": 2}))
    assert ordinary.first == 1 and ordinary.second == 2

    class Holder(Model):
        detail: Value

    identity = str(ObjectId())
    mapping = type_name(Holder)
    wire = {f"{name}_wire": getattr(ordinary, name) for name, _ in fields}
    graph = {"roots": [{"mapping": mapping, "id": identity}], "nodes": [{
        "mapping": mapping, "id": identity, "value": {"id": identity, "detail": wire},
    }]}
    restored = decode_graph(graph, {mapping: Holder}.__getitem__)[0]
    assert restored.detail.first == 1 and restored.detail.second == 2
