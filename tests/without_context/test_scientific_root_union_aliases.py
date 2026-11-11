"""Scientific value composition preserves typed roots and GUI wire aliases."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

import pytest
from pydantic import AliasChoices, BaseModel, Field, RootModel, field_validator

from simstack.models.document import Model, ObjectId
from simstack.models.document.codec import encode_document_value
from simstack.server_api.scientific_value_graph import decode_graph, encode_graph, type_name


def gui_graph(cls, detail):
    identity = str(ObjectId())
    mapping = type_name(cls)
    return {"roots": [{"mapping": mapping, "id": identity}], "nodes": [{
        "mapping": mapping, "id": identity, "value": {"id": identity, "detail": encode_document_value(detail)}}]}


@pytest.mark.parametrize("annotation,value", [
    (int, 1000001), (list[float], [1.25, 2.5]),
    (dict[str, Decimal], {"root": Decimal("3.2500")}),
    (tuple[int, Decimal], (7, Decimal("2.50"))),
])
def test_rootmodel_direct_and_gui_values_preserve_root_shape(annotation, value):
    class RootValue(RootModel[annotation]):
        pass
    class Holder(Model):
        detail: RootValue
    source = Holder(detail=RootValue(value))
    types = {type_name(cls): cls for cls in (RootValue, Holder)}
    restored = decode_graph(encode_graph([source]), types.__getitem__)[0]
    assert type(restored.detail) is RootValue
    assert type(restored.detail.root) is type(value)
    assert restored.detail.root == value
    assert decode_graph(gui_graph(Holder, value), types.__getitem__)[0].detail.root == value


def test_rootmodel_nested_plain_aliases_keep_schema_and_validation_order():
    events = []
    class Value(BaseModel):
        amount: int = Field(validation_alias="input", serialization_alias="energy")
        @field_validator("amount", mode="before")
        @classmethod
        def child(cls, value):
            events.append("child")
            return value
    class Values(RootModel[list[Value]]):
        pass
    class Holder(Model):
        detail: Values
        @field_validator("detail", mode="before")
        @classmethod
        def parent(cls, value):
            events.append("parent")
            assert isinstance(value, list)
            return value
    restored = decode_graph(gui_graph(Holder, [{"energy": 7}]), {type_name(Holder): Holder}.__getitem__)[0]
    assert restored.detail.root[0].amount == 7
    assert events == ["parent", "child"]
    schema = Holder.model_json_schema()
    assert schema["$defs"]["Value"]["required"] == ["energy"]
    assert set(schema["$defs"]["Value"]["properties"]) == {"energy"}


@pytest.mark.parametrize("container", [False, True])
@pytest.mark.parametrize("aliased_tag", [False, True])
def test_discriminated_plain_union_aliases_keep_branch_and_validator_order(container, aliased_tag):
    events = []
    class Energy(BaseModel):
        kind: Literal["energy"] = Field(alias="input_kind", serialization_alias="wire_kind") if aliased_tag else Field()
        amount: int = Field(validation_alias="input_amount", serialization_alias="energy")
        @field_validator("amount", mode="before")
        @classmethod
        def child(cls, value):
            events.append("child")
            return value
    class Count(BaseModel):
        kind: Literal["count"] = Field(alias="input_kind", serialization_alias="wire_kind") if aliased_tag else Field()
        amount: int = Field(validation_alias="input_count", serialization_alias="count")
    variant = Annotated[Energy | Count, Field(discriminator="kind")]
    class Holder(Model):
        detail: list[variant] if container else variant
        @field_validator("detail", mode="before")
        @classmethod
        def parent(cls, value):
            events.append("parent")
            assert isinstance(value, list if container else dict)
            return value
    wire = {"wire_kind" if aliased_tag else "kind": "energy", "energy": 7}
    restored = decode_graph(gui_graph(Holder, [wire] if container else wire), {type_name(Holder): Holder}.__getitem__)[0]
    value = restored.detail[0] if container else restored.detail
    assert type(value) is Energy
    assert value.amount == 7
    assert events == ["parent", "child"]
    schema = Holder.model_json_schema()
    branch = schema["properties"]["detail"]
    if container:
        branch = branch["items"]
    assert branch["discriminator"]["propertyName"] == ("wire_kind" if aliased_tag else "kind")


@pytest.mark.parametrize("value", [1000001, Decimal("3.2500"), b"scientific", [1000001, {"amount": Decimal("3.25")} ]])
@pytest.mark.parametrize("choices", [False, True])
def test_equal_shared_aliases_accept_separately_decoded_typed_values(value, choices):
    class Value(BaseModel):
        first: Any = Field(validation_alias=AliasChoices("shared", "left") if choices else "shared", serialization_alias="first_wire")
        second: Any = Field(validation_alias=AliasChoices("shared", "right") if choices else "shared", serialization_alias="second_wire")
    class Holder(Model):
        detail: Value
    restored = decode_graph(gui_graph(Holder, {"first_wire": value, "second_wire": value}), {type_name(Holder): Holder}.__getitem__)[0]
    assert restored.detail.first == restored.detail.second == value
    assert type(restored.detail.first) is type(value)


@pytest.mark.parametrize("first,second", [
    (True, 1), (Decimal("1"), 1.0), ([True], [1]),
    ({"v": True}, {"v": 1}), (1, 2),
    (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 1, tzinfo=timezone(timedelta(hours=1)))),
])
def test_shared_aliases_do_not_merge_distinct_scientific_values(first, second):
    class Value(BaseModel):
        first: Any = Field(validation_alias="shared", serialization_alias="first_wire")
        second: Any = Field(validation_alias="shared", serialization_alias="second_wire")
    class Holder(Model):
        detail: Value
    with pytest.raises(ValueError, match="Overlapping scientific validation alias"):
        decode_graph(gui_graph(Holder, {"first_wire": first, "second_wire": second}), {type_name(Holder): Holder}.__getitem__)


@pytest.mark.parametrize("strict,reverse,choices", [(False, False, False), (True, False, False), (True, True, False), (True, True, True)])
def test_shared_numeric_alias_uses_input_that_preserves_each_declared_type(strict, reverse, choices):
    from pydantic import create_model
    order = [("first", int, 2), ("second", float, 2.0)]
    if reverse:
        order.reverse()
    Value = create_model("SharedNumbers", __config__={"strict": strict}, **{
        name: (kind, Field(validation_alias=AliasChoices("shared", name) if choices else "shared", serialization_alias=name + "_wire"))
        for name, kind, _ in order})
    class Holder(Model):
        detail: Value
    original = Value(shared=2)
    restored = decode_graph(gui_graph(Holder, {name + "_wire": value for name, _, value in order}), {type_name(Holder): Holder}.__getitem__)[0]
    assert restored.detail == original
    assert type(restored.detail.first) is int
    assert type(restored.detail.second) is float


def test_shared_bool_and_int_alias_preserves_valid_constructor_coercion():
    class Value(BaseModel):
        first: bool = Field(validation_alias="shared", serialization_alias="first_wire")
        second: int = Field(validation_alias="shared", serialization_alias="second_wire")
    class Holder(Model):
        detail: Value
    restored = decode_graph(gui_graph(Holder, {"first_wire": True, "second_wire": 1}), {type_name(Holder): Holder}.__getitem__)[0]
    assert restored.detail == Value(shared=True)
    assert type(restored.detail.first) is bool
    assert type(restored.detail.second) is int


def test_discriminator_object_with_field_name_decodes_wire_aliases():
    from pydantic import Discriminator
    class Energy(BaseModel):
        kind: Literal["energy"]
        amount: int = Field(validation_alias="input_amount", serialization_alias="energy")
    class Count(BaseModel):
        kind: Literal["count"]
        count: int
    class Holder(Model):
        detail: Annotated[Energy | Count, Discriminator("kind")]
    restored = decode_graph(gui_graph(Holder, {"kind": "energy", "energy": 7}), {type_name(Holder): Holder}.__getitem__)[0]
    assert restored.detail.amount == 7


def test_shared_typed_containers_use_compatible_constructor_values():
    class Value(BaseModel):
        first: list[int] = Field(validation_alias="shared", serialization_alias="first_wire")
        second: list[float] = Field(validation_alias="shared", serialization_alias="second_wire")
    class Holder(Model):
        detail: Value
    restored = decode_graph(gui_graph(Holder, {"first_wire": [2], "second_wire": [2.0]}), {type_name(Holder): Holder}.__getitem__)[0]
    assert restored.detail == Value(shared=[2])
    assert type(restored.detail.first[0]) is int
    assert type(restored.detail.second[0]) is float


def test_callable_discriminator_keeps_parent_wrap_and_before_validator_order():
    from pydantic import Discriminator, Tag
    events = []
    class Energy(BaseModel):
        kind: Literal["energy"]
        amount: int = Field(validation_alias="input_amount", serialization_alias="energy")
        @field_validator("amount", mode="before")
        @classmethod
        def child(cls, value):
            events.append("child")
            return value
    class Count(BaseModel):
        kind: Literal["count"]
        count: int
    def choose(value):
        events.append("discriminator")
        return value["kind"]
    variant = Annotated[Annotated[Energy, Tag("energy")] | Annotated[Count, Tag("count")], Discriminator(choose)]
    class Holder(Model):
        detail: variant
        @field_validator("detail", mode="before")
        @classmethod
        def parent(cls, value):
            events.append("parent")
            assert isinstance(value, dict)
            value["input_amount"] += 1
            return value
        @field_validator("detail", mode="wrap")
        @classmethod
        def wrapped(cls, value, handler):
            events.append("wrap enter")
            result = handler(value)
            events.append("wrap exit")
            return result
    restored = decode_graph(gui_graph(Holder, {"kind": "energy", "energy": 7}), {type_name(Holder): Holder}.__getitem__)[0]
    assert type(restored.detail) is Energy
    assert restored.detail.amount == 8
    assert events == ["wrap enter", "parent", "discriminator", "child", "wrap exit"]


def test_named_enum_literal_discriminator_uses_its_wire_value():
    from enum import Enum
    class Kind(str, Enum):
        ENERGY = "energy"
        COUNT = "count"
    class Energy(BaseModel):
        kind: Literal[Kind.ENERGY]
        amount: int = Field(validation_alias="input_amount", serialization_alias="energy")
    class Count(BaseModel):
        kind: Literal[Kind.COUNT]
        count: int
    class Holder(Model):
        detail: Annotated[Energy | Count, Field(discriminator="kind")]
    restored = decode_graph(gui_graph(Holder, {"kind": "energy", "energy": 7}), {type_name(Holder): Holder}.__getitem__)[0]
    assert restored.detail.kind is Kind.ENERGY
    assert restored.detail.amount == 7


def test_callable_discriminator_without_literals_uses_unambiguous_wire_fields_once():
    from pydantic import Discriminator, Tag
    events = []
    class Energy(BaseModel):
        amount: int = Field(validation_alias="input_amount", serialization_alias="energy")
    class Count(BaseModel):
        count: int = Field(validation_alias="input_count", serialization_alias="count")
    def choose(value):
        events.append("discriminator")
        return "energy" if isinstance(value, Energy) or any(key in value for key in ("energy", "amount", "input_amount")) else "count"
    variant = Annotated[Annotated[Energy, Tag("energy")] | Annotated[Count, Tag("count")], Discriminator(choose)]
    class Holder(Model):
        detail: variant
        @field_validator("detail", mode="before")
        @classmethod
        def parent(cls, value):
            events.append("parent")
            assert isinstance(value, dict)
            return value
    restored = decode_graph(gui_graph(Holder, {"energy": 7}), {type_name(Holder): Holder}.__getitem__)[0]
    assert type(restored.detail) is Energy
    assert restored.detail.amount == 7
    assert events == ["parent", "discriminator"]


@pytest.mark.parametrize("positive", [False, True])
def test_callable_identical_alias_forms_preserve_predicate_choice_and_order(positive):
    from pydantic import Discriminator, Tag
    events = []
    class Positive(BaseModel):
        model_config = {"populate_by_name": True}
        energy: float = Field(validation_alias="input_energy", serialization_alias="wire_energy")
    class Negative(BaseModel):
        model_config = {"populate_by_name": True}
        energy: float = Field(validation_alias="input_energy", serialization_alias="wire_energy")
    def sign(value):
        events.append("discriminator")
        return "positive" if value["energy"] >= 0 else "negative"
    variant = Annotated[Annotated[Positive, Tag("positive")] | Annotated[Negative, Tag("negative")], Discriminator(sign)]
    class Holder(Model):
        detail: variant
        @field_validator("detail", mode="before")
        @classmethod
        def parent(cls, value):
            assert isinstance(value, dict)
            events.append("parent")
            return value
        @field_validator("detail", mode="wrap")
        @classmethod
        def wrapped(cls, value, handler):
            events.append("wrap enter")
            result = handler(value)
            events.append("wrap exit")
            return result
    energy = 3.25 if positive else -3.25
    restored = decode_graph(gui_graph(Holder, {"wire_energy": energy}), {type_name(Holder): Holder}.__getitem__)[0]
    assert type(restored.detail) is (Positive if positive else Negative)
    assert restored.detail.energy == energy
    assert events == ["wrap enter", "parent", "discriminator", "wrap exit"]


def test_callable_different_alias_forms_do_not_pick_or_coerce_a_branch():
    from pydantic import Discriminator, Tag
    observed = []
    class First(BaseModel):
        energy: float = Field(validation_alias="first_input", serialization_alias="wire_energy")
    class Second(BaseModel):
        energy: float = Field(validation_alias="second_input", serialization_alias="wire_energy")
    def choose(value):
        observed.append(dict(value))
        return "first"
    variant = Annotated[Annotated[First, Tag("first")] | Annotated[Second, Tag("second")], Discriminator(choose)]
    class Holder(Model):
        detail: variant
    with pytest.raises(ValueError, match="first_input"):
        decode_graph(gui_graph(Holder, {"wire_energy": 3.25}), {type_name(Holder): Holder}.__getitem__)
    assert observed == [{"wire_energy": 3.25}]


def test_patterned_maps_publish_nested_wire_aliases_and_reference_markers():
    from pydantic import StringConstraints
    from simstack.models.document import Reference
    key = Annotated[str, StringConstraints(pattern="^[a-z_]+$")]
    class Reading(BaseModel):
        label: str = Field(validation_alias="input_label", serialization_alias="__simstack_model__")
    class Sample(Model):
        label: str
    class Holder(Model):
        readings: dict[key, list[dict[key, Reading]]]
        references: dict[key, Sample] = Reference()
    schema = Holder.model_json_schema()
    assert set(schema["$defs"]["Reading"]["properties"]) == {"__simstack_model__"}
    child = schema["properties"]["references"]["patternProperties"]["^[a-z_]+$"]
    assert child["x-simstack-reference"] is True
    assert child["x-simstack-reference-model"] == "Sample"
