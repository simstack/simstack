import json
import subprocess
import sys
from typing import Annotated, Literal, Optional, Union

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, Field

from simstack.models import simstack_model
from simstack.models.document import EmbeddedModel, Model
from simstack.models.files import FileStack
from simstack.util.generate_ui_schema import generate_ui_schema, resolve_ui_schema


def test_public_generator_module_can_be_imported_before_the_models_package():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from simstack.util.generate_ui_schema import generate_ui_schema, resolve_ui_schema",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_generate_ui_schema_inlines_decorated_embedded_models():
    @simstack_model
    class NestedPluginModel(EmbeddedModel):
        value: str = "default"

    @simstack_model
    class ParentModel(Model):
        nested: NestedPluginModel

    ui_schema = ParentModel.ui_schema()

    assert "nested" not in ui_schema


def test_generate_ui_schema_uses_persisted_nested_model_import_path():
    @simstack_model
    class NestedPluginModel(Model):
        value: str = "default"

    @simstack_model
    class ParentModel(Model):
        nested: NestedPluginModel

    assert ParentModel.ui_schema()["nested"] == {
        "ui:field": "GenericFormField",
        "ui:options": {
            "model": f"{NestedPluginModel.__module__}.NestedPluginModel",
            "accordion": "true",
        },
    }


def test_plain_model_uses_nested_custom_components_without_a_decorator():
    class CustomValue(EmbeddedModel):
        value: str

        @classmethod
        def ui_schema(cls):
            return {"ui:field": "PluginValueField", "ui:options": {"style": "short"}}

    class Embedded(BaseModel):
        attachment: FileStack
        custom: CustomValue

    class Referenced(Model):
        attachment: FileStack

    class Plain(Model):
        file: FileStack
        embedded: Embedded
        referenced: Referenced

    schema = resolve_ui_schema(Plain)
    assert schema["file"]["ui:field"] == "FileField"
    assert schema["embedded"]["attachment"]["ui:field"] == "FileField"
    assert schema["embedded"]["custom"] == CustomValue.ui_schema()
    assert schema["referenced"] == {
        "ui:field": "GenericFormField",
        "ui:options": {
            "model": f"{Referenced.__module__}.Referenced",
            "accordion": "true",
        },
    }
    assert schema["id"] == {"ui:widget": "hidden"}
    assert "locations" not in schema["file"]


def test_nullable_and_container_annotations_keep_the_model_component_at_each_leaf():
    class Plain(Model):
        nullable: FileStack | None
        optional: Optional[FileStack]
        reversed_optional: Union[None, FileStack]
        files: list[Optional[FileStack]]
        by_name: dict[str, list[FileStack | None]] | None
        annotated: Annotated[FileStack, "public"]
        pair: tuple[FileStack, str]
        repeated: tuple[FileStack, ...]

    schema = resolve_ui_schema(Plain)
    for name in ("nullable", "optional", "reversed_optional"):
        assert schema[name]["ui:field"] == "FileField"
        assert schema[name]["ui:fieldReplacesAnyOrOneOf"] is True
    assert schema["files"]["items"]["ui:field"] == "FileField"
    assert schema["by_name"]["additionalProperties"]["items"]["ui:field"] == "FileField"
    assert schema["annotated"]["ui:field"] == "FileField"
    assert schema["pair"]["items"][0]["ui:field"] == "FileField"
    assert schema["pair"]["items"][1] == {}
    assert schema["repeated"]["items"]["ui:field"] == "FileField"


def test_heterogeneous_union_presentation_matches_each_json_schema_branch():
    class Plain(Model):
        value: Union[None, str, FileStack]

    schema = resolve_ui_schema(Plain)
    assert schema["value"]["anyOf"][0] == {}
    assert schema["value"]["anyOf"][1]["ui:field"] == "FileField"
    assert schema["value"]["anyOf"][2] == {}
    assert len(schema["value"]["anyOf"]) == len(
        Plain.model_json_schema()["properties"]["value"]["anyOf"]
    )


def test_discriminated_union_uses_one_of_presentation():
    class First(BaseModel):
        kind: Literal["first"]
        attachment: FileStack

    class Second(BaseModel):
        kind: Literal["second"]
        text: str

    class Plain(Model):
        value: First | Second = Field(discriminator="kind")
        values: list[Annotated[First | Second, Field(discriminator="kind")]]
        by_key: dict[str, Annotated[First | Second, Field(discriminator="kind")]]
        optional: First | Second | None = Field(discriminator="kind")

    schema = resolve_ui_schema(Plain)
    assert schema["value"]["oneOf"][0]["attachment"]["ui:field"] == "FileField"
    assert schema["value"]["oneOf"][1] == {}
    assert "oneOf" in Plain.model_json_schema()["properties"]["value"]
    assert (
        schema["values"]["items"]["oneOf"][0]["attachment"]["ui:field"] == "FileField"
    )
    assert (
        schema["by_key"]["additionalProperties"]["oneOf"][0]["attachment"]["ui:field"]
        == "FileField"
    )
    assert (
        schema["optional"]["anyOf"][0]["oneOf"][0]["attachment"]["ui:field"]
        == "FileField"
    )
    assert schema["optional"]["anyOf"][1] == {}


def test_explicit_ui_schema_is_preserved_including_empty_and_none():
    for explicit in (
        {},
        {"ui:field": "PluginForm", "ui:options": {"compact": True}},
        None,
    ):

        class Custom(Model):
            file: FileStack

            @classmethod
            def ui_schema(cls):
                return explicit

        assert resolve_ui_schema(Custom) == explicit


def test_ui_base_schema_field_overrides_are_not_mutated_or_replaced():
    base = {
        "ui:order": ["attachment", "other", "id"],
        "attachment": {"ui:field": "PluginFileField"},
        "id": {"ui:widget": "custom-id"},
    }

    class Plain(Model):
        attachment: FileStack
        other: FileStack

        @classmethod
        def ui_base_schema(cls):
            return base

    schema = resolve_ui_schema(Plain)
    assert schema["attachment"] == base["attachment"]
    assert schema["id"] == base["id"]
    assert schema["other"]["ui:field"] == "FileField"
    schema["attachment"]["ui:field"] = "Changed"
    assert base["attachment"]["ui:field"] == "PluginFileField"
    assert "other" not in base


def test_field_aliases_follow_the_json_schema_names():
    class Plain(Model):
        attachment: FileStack = Field(alias="source")
        second_attachment: FileStack = Field(validation_alias="other_source")
        choices: FileStack = Field(
            validation_alias=AliasChoices("primary", "secondary")
        )
        path_choices: FileStack = Field(
            validation_alias=AliasChoices(AliasPath("nested", "source"), "flat_source")
        )
        single_path: FileStack = Field(validation_alias=AliasPath("single_source"))

    assert "source" in Plain.model_json_schema()["properties"]
    assert resolve_ui_schema(Plain)["source"]["ui:field"] == "FileField"
    assert "attachment" not in resolve_ui_schema(Plain)
    assert resolve_ui_schema(Plain)["other_source"]["ui:field"] == "FileField"
    for name in ("primary", "flat_source", "single_path"):
        assert name in Plain.model_json_schema()["properties"]
        assert resolve_ui_schema(Plain)[name]["ui:field"] == "FileField"


def test_recursive_models_generate_bounded_ui_without_losing_leaf_components():
    class RecursiveEmbedded(EmbeddedModel):
        attachment: FileStack
        children: list["RecursiveEmbedded"] = []

    class Recursive(Model):
        embedded: RecursiveEmbedded
        children: list["Recursive"] = []

    schema = resolve_ui_schema(Recursive)
    assert schema["embedded"]["attachment"]["ui:field"] == "FileField"
    recursive_ui = schema["embedded"]["children"]["items"]
    assert recursive_ui["ui:field"] == "RecursiveFormField"
    definition = recursive_ui["ui:options"]["definition"]
    embedded_schema = schema["ui:definitions"][definition]
    assert embedded_schema["attachment"]["ui:field"] == "FileField"
    assert embedded_schema["children"]["items"] == recursive_ui
    assert schema["children"]["items"]["ui:field"] == "GenericFormField"
    assert len(json.dumps(schema)) < 2000


def test_mutually_recursive_embedded_ui_definitions_are_finite_and_root_local():
    class First(EmbeddedModel):
        file: FileStack
        second: "Second | None" = None

    class Second(EmbeddedModel):
        first: First | None = None

    First.model_rebuild()

    class Parent(Model):
        first: First

    schema = resolve_ui_schema(Parent)
    recursive_ui = schema["first"]["second"]["first"]
    assert recursive_ui["ui:field"] == "RecursiveFormField"
    assert recursive_ui["ui:fieldReplacesAnyOrOneOf"] is True
    definition = recursive_ui["ui:options"]["definition"]
    assert schema["ui:definitions"][definition]["file"]["ui:field"] == "FileField"
    assert len(json.dumps(schema)) < 3000

    class Unrelated(Model):
        file: FileStack

    assert "ui:definitions" not in resolve_ui_schema(Unrelated)


def test_existing_ui_definitions_and_explicit_recursive_field_override_are_preserved():
    custom = {"author": {"ui:description": "Preserved definition"}}

    class Embedded(EmbeddedModel):
        file: FileStack
        children: list["Embedded"] = []

    class Parent(Model):
        nested: Embedded

        @classmethod
        def ui_base_schema(cls):
            return {"ui:definitions": custom}

    schema = resolve_ui_schema(Parent)
    assert schema["ui:definitions"]["author"] == custom["author"]
    assert len(schema["ui:definitions"]) == 2
    assert custom == {"author": {"ui:description": "Preserved definition"}}

    class Explicit(EmbeddedModel):
        children: list["Explicit"] = []

        @classmethod
        def ui_base_schema(cls):
            return {"children": {"ui:field": "AuthorTreeField"}}

    assert resolve_ui_schema(Explicit) == {"children": {"ui:field": "AuthorTreeField"}}


@pytest.mark.parametrize(
    "invalid", [[], "schema", {"value": float("nan")}, {"value": object()}]
)
def test_invalid_custom_ui_schemas_raise_for_the_callers_to_report(invalid):
    class Custom(Model):
        @classmethod
        def ui_schema(cls):
            return invalid

    with pytest.raises((TypeError, ValueError)):
        resolve_ui_schema(Custom)


def test_invalid_ui_base_schema_does_not_poison_later_generation():
    class Invalid(Model):
        @classmethod
        def ui_base_schema(cls):
            return []

    class Valid(Model):
        file: FileStack

    with pytest.raises(ValueError, match="UI base schema"):
        generate_ui_schema(Invalid)
    assert resolve_ui_schema(Valid)["file"]["ui:field"] == "FileField"
