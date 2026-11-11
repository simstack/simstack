"""Scientific field aliases never occupy the API's record identity slot."""
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field as PydanticField, computed_field, field_serializer, model_serializer, model_validator

from simstack.models.document import EmbeddedModel, Field, Model, Reference
from simstack.server_api.scientific_value_graph import decode_graph, encode_graph, type_name


class IdentityAlias(Model):
    label: str = Field(key_name="id")
    energy: float


class SuffixIdentityAlias(IdentityAlias):
    suffix: str = Field(key_name="id_")
    id__: str


class CustomIdentityAlias(Model):
    sample_key: int = Field(primary_field=True)
    label: str = Field(key_name="id")


class ComputedIdentityAlias(IdentityAlias):
    @computed_field(alias="id_")
    @property
    def display_label(self) -> str:
        return self.label.upper()


class ComputedPrimaryAlias(Model):
    model_config = ConfigDict(extra="forbid")
    label: str

    @computed_field(alias="id")
    @property
    def display_label(self) -> str:
        return self.label.upper()


class OrdinaryComputed(Model):
    model_config = ConfigDict(extra="forbid")
    label: str

    @computed_field
    @property
    def display(self) -> str:
        return self.label.upper()


class ComputedNameAlias(Model):
    label: str = Field(key_name="display")

    @computed_field(alias="derived")
    @property
    def display(self) -> str:
        return self.label.upper()


class DefaultComputedNameAlias(ComputedNameAlias):
    label: str = Field(default="fallback", key_name="display")


@pytest.mark.parametrize("cls", [ComputedNameAlias, DefaultComputedNameAlias])
def test_computed_python_name_does_not_discard_a_declared_scientific_wire_alias(cls):
    class Owner(Model):
        embedded: cls
        referenced: cls = Reference()

    child = cls(label="scientific-sample")
    source = Owner(embedded=child, referenced=child)
    types = {type_name(Owner): Owner, type_name(cls): cls}
    restored = decode_graph(encode_graph([source]), types.__getitem__)[0]
    assert restored.model_dump() == source.model_dump()
    assert restored.embedded.label == restored.referenced.label == "scientific-sample"
    assert restored.embedded.display == restored.referenced.display == "SCIENTIFIC-SAMPLE"
    assert cls.model_validate_doc(child.model_dump_doc()).label == child.label
    assert cls.model_json_schema()["x-simstack-computed-fields"] == ["derived"]


def test_nested_computed_metadata_describes_models_without_marking_literal_maps():
    class Embedded(EmbeddedModel):
        model_config = ConfigDict(extra="forbid")
        value: int

        @computed_field(alias="derived")
        @property
        def doubled(self) -> int:
            return self.value * 2

    class Plain(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int

        @computed_field
        @property
        def doubled(self) -> int:
            return self.value * 2

    class Owner(Model):
        embedded: Embedded
        plain: Plain
        samples: dict[str, Embedded]
        literal: dict[str, int]

    source = Owner(embedded=Embedded(value=7), plain=Plain(value=7),
                   samples={"science": Embedded(value=7)}, literal={"doubled": 99})
    for mode in ("validation", "serialization"):
        schema = Owner.model_json_schema(mode=mode)
        assert schema["$defs"]["Embedded"]["x-simstack-computed-fields"] == ["derived"]
        assert schema["$defs"]["Plain"]["x-simstack-computed-fields"] == ["doubled"]
        assert "x-simstack-computed-fields" not in schema["properties"]["literal"]
        assert "x-simstack-computed-fields" not in schema["properties"]["samples"]
    types = {type_name(cls): cls for cls in (Owner, Embedded, Plain)}
    assert decode_graph(encode_graph([source]), types.__getitem__)[0].model_dump() == source.model_dump()


def test_custom_serializer_keeps_previously_accepted_computed_python_name_output():
    class Sample(Model):
        model_config = ConfigDict(extra="forbid")
        label: str

        @computed_field(alias="derived")
        @property
        def display(self) -> str:
            return self.label.upper()

        @model_serializer(mode="wrap")
        def custom_output(self, handler):
            value = handler(self)
            if "derived" in value:
                value["display"] = value.pop("derived")
            return value

    source = Sample(label="scientific-sample")
    restored = decode_graph(encode_graph([source]), lambda _: Sample)[0]
    assert restored.id == source.id and restored.label == source.label
    assert restored.display == source.display


@pytest.mark.parametrize("source,wire", [
    (IdentityAlias(label="sample-A", energy=3.25), "id_"),
    (SuffixIdentityAlias(label="sample-A", energy=3.25, suffix="suffix", id__="name"), "id___"),
    (CustomIdentityAlias(sample_key=27, label="sample-A"), "id_"),
    (ComputedIdentityAlias(label="sample-A", energy=3.25), "id__"),
])
def test_identity_and_scientific_alias_have_distinct_schema_graph_and_document_fields(source, wire):
    cls = type(source)
    for mode in ("validation", "serialization"):
        for by_alias in (True, False):
            schema = cls.model_json_schema(mode=mode, by_alias=by_alias)
            assert schema["x-simstack-field-aliases"]["label"] == wire
            assert schema["properties"][wire]["type"] == "string"
            assert wire in schema["required"]
            assert schema["properties"]["id"].get("format") == (None if cls is CustomIdentityAlias else "objectid")
    graph = encode_graph([source])
    assert graph["nodes"][0]["value"][wire] == "sample-A"
    assert graph["nodes"][0]["value"]["id"] == source.id
    if cls is ComputedIdentityAlias:
        assert graph["nodes"][0]["value"]["id_"] == source.display_label
    assert decode_graph(graph, {type_name(cls): cls}.__getitem__)[0].model_dump() == source.model_dump()
    document = source.model_dump_doc()
    assert document["id"] == source.id and document[wire] == "sample-A"
    assert cls.model_validate_doc(document).model_dump() == source.model_dump()


class ValidatedIdentityAlias(Model):
    calls: ClassVar[list[tuple[str, str]]] = []
    label: str = PydanticField(validation_alias="input_label", serialization_alias="id")

    @model_validator(mode="before")
    @classmethod
    def before(cls, values):
        cls.calls.append(("before", values.get("label", values.get("input_label"))))
        return values

    @model_validator(mode="after")
    def after(self):
        type(self).calls.append(("after", self.label))
        assert self.label == "SAMPLE"
        return self

    @field_serializer("label")
    def serialize_label(self, label):
        return label


def test_transport_does_not_mutate_public_aliases_or_repeat_user_validation():
    source = ValidatedIdentityAlias(input_label="SAMPLE")
    graph = encode_graph([source])
    ValidatedIdentityAlias.calls.clear()
    restored = decode_graph(graph, {type_name(type(source)): type(source)}.__getitem__)[0]
    assert restored.id == source.id and restored.label == "SAMPLE"
    assert ValidatedIdentityAlias.calls == [("before", "SAMPLE"), ("after", "SAMPLE")]
    field = ValidatedIdentityAlias.model_fields["label"]
    assert field.validation_alias == "input_label" and field.serialization_alias == "id"
    assert source.model_dump(by_alias=True) == {"id": "SAMPLE"}
    assert source.model_dump()["label"] == "SAMPLE"
    assert ValidatedIdentityAlias(label="SAMPLE").label == "SAMPLE"


def test_nested_document_alias_preserves_identity_and_validator_order():
    class Owner(Model):
        detail: ValidatedIdentityAlias

    original = Owner(detail=ValidatedIdentityAlias(input_label="SAMPLE"))
    ValidatedIdentityAlias.calls.clear()
    restored = Owner.model_validate_doc(original.model_dump_doc())
    assert restored.model_dump() == original.model_dump()
    assert ValidatedIdentityAlias.calls == [("before", "SAMPLE"), ("after", "SAMPLE")]


def test_computed_identity_alias_preserves_primary_identity_and_computed_value():
    source = ComputedPrimaryAlias(label="sample")
    graph = encode_graph([source])
    assert graph["nodes"][0]["value"] == {"id": source.id, "label": "sample", "id_": "SAMPLE"}
    restored = decode_graph(graph, {type_name(type(source)): type(source)}.__getitem__)[0]
    assert restored.id == source.id and restored.display_label == "SAMPLE"
    schema = ComputedPrimaryAlias.model_json_schema(mode="serialization")
    assert schema["properties"]["id"]["format"] == "objectid"
    assert schema["properties"]["id_"]["readOnly"] is True
    assert ComputedPrimaryAlias.model_validate_doc(source.model_dump_doc()).id == source.id


@pytest.mark.parametrize("cls,computed", [(ComputedPrimaryAlias, "id_"), (OrdinaryComputed, "display")])
def test_strict_computed_document_roundtrip_and_published_metadata(cls, computed):
    source = cls(label="sample")
    for mode in ("validation", "serialization"):
        for by_alias in (True, False):
            schema = cls.model_json_schema(mode=mode, by_alias=by_alias)
            assert schema["x-simstack-computed-fields"] == [computed]
            assert schema["additionalProperties"] is False
            if mode == "serialization":
                assert schema["properties"][computed]["readOnly"] is True
    restored = cls.model_validate_doc(source.model_dump_doc())
    assert restored.model_dump() == source.model_dump()
    with pytest.raises(ValueError, match="Could not parse"):
        cls.model_validate_doc({**source.model_dump_doc(), "unrecognized": "not derived"})


def test_computed_metadata_does_not_mark_declared_science_or_literal_maps():
    class ScientificAlias(Model):
        label: str = Field(key_name="display")

        @computed_field(alias="display")
        @property
        def derived(self) -> str:
            return self.label.upper()

    class LiteralValue(EmbeddedModel):
        data: dict[str, str]

        @computed_field
        @property
        def display(self) -> str:
            return "value object"

    class Owner(Model):
        referenced: ComputedPrimaryAlias = Reference()
        literal: LiteralValue

    assert "x-simstack-computed-fields" not in ScientificAlias.model_json_schema()
    assert ScientificAlias.model_validate_doc({"display": "scientific value"}).label == "scientific value"
    schema = Owner.model_json_schema()
    assert schema["$defs"]["ComputedPrimaryAlias"]["x-simstack-computed-fields"] == ["id_"]
    assert schema["$defs"]["LiteralValue"]["x-simstack-computed-fields"] == ["display"]
    assert "x-simstack-computed-fields" not in schema["$defs"]["LiteralValue"]["properties"]["data"]


def test_document_normalizes_before_user_wrap_and_before_without_early_nested_validation():
    calls = []

    class Child(Model):
        label: str = Field(key_name="id")

        @model_validator(mode="wrap")
        @classmethod
        def wrap(cls, values, handler):
            if isinstance(values, cls):
                return handler(values)
            calls.append(("child-wrap", values["label"]))
            result = handler(values)
            calls.append(("child-wrap-return", result.label))
            return result

        @model_validator(mode="before")
        @classmethod
        def before(cls, values):
            calls.append(("child-before", values["label"]))
            return {**values, "label": values["label"].strip().upper()}

    class Parent(Model):
        detail: Child

        @model_validator(mode="before")
        @classmethod
        def before(cls, values):
            calls.append(("parent-before", isinstance(values["detail"], dict)))
            return values

        @model_validator(mode="after")
        def after(self):
            calls.append(("parent-after", self.detail.label))
            return self

    original = Parent(detail=Child(label="sample"))
    document = original.model_dump_doc()
    document["detail"]["id_"] = " sample "
    calls.clear()
    restored = Parent.model_validate_doc(document)
    assert restored.model_dump() == original.model_dump()
    assert calls == [("parent-before", True), ("child-wrap", " sample "),
                     ("child-before", " sample "), ("child-wrap-return", "SAMPLE"),
                     ("parent-after", "SAMPLE")]
