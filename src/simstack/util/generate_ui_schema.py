"""Default presentation for domain models, independent of their decorators."""

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from copy import deepcopy
import json
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import AliasChoices, BaseModel

_generating_models: ContextVar[frozenset[type]] = ContextVar(
    "generating_ui_schema_models", default=frozenset()
)
_embedded_definitions: ContextVar[dict[str, dict[str, Any] | None] | None] = ContextVar(
    "generating_embedded_ui_definitions", default=None
)


def resolve_ui_schema(cls: type[BaseModel]) -> dict[str, Any] | None:
    """Use a model's explicit presentation, or generate the common default.

    Discovery and revision-specific runtime requests use the same rule. Custom
    hooks remain optional: callers decide whether a hook failure is a warning or
    a request error. A successful hook must return a JSON object (or ``None``).
    """
    method = getattr(cls, "ui_schema", None)
    schema = method() if callable(method) else generate_ui_schema(cls)
    if schema is not None:
        if not isinstance(schema, dict):
            raise ValueError("UI schema must be a JSON object")
        json.dumps(schema, allow_nan=False)
    return schema


def _model_reference(cls: type[BaseModel], *, accordion: bool) -> dict[str, Any]:
    options = {"model": f"{cls.__module__}.{cls.__name__}"}
    if accordion:
        options["accordion"] = "true"
    return {"ui:field": "GenericFormField", "ui:options": options}


def _field_schema(
    annotation: Any,
    *,
    accordion: bool = True,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Follow annotation containers to their actual model presentation."""
    json_schema = json_schema or {}
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _field_schema(arguments[0], accordion=accordion, json_schema=json_schema)
    if origin in (Union, UnionType):
        choices = [choice for choice in arguments if choice is not type(None)]
        branches = json_schema.get("anyOf", [])
        non_null_branches = [
            branch for branch in branches if branch.get("type") != "null"
        ]
        if len(choices) == 1:
            child_schema = (
                non_null_branches[0] if len(non_null_branches) == 1 else json_schema
            )
            schema = _field_schema(
                choices[0], accordion=accordion, json_schema=child_schema
            )
            if "ui:field" in schema:
                schema["ui:fieldReplacesAnyOrOneOf"] = True
            return schema
        # Pydantic groups a nullable discriminated union as anyOf(oneOf(...),
        # null), even though typing presents its variants as one flat union.
        if len(non_null_branches) == 1 and "oneOf" in non_null_branches[0]:
            child = _field_schema(
                Union[tuple(choices)],
                accordion=accordion,
                json_schema=non_null_branches[0],
            )
            return {"anyOf": [child, {}]} if child else {}
        key = "oneOf" if "oneOf" in json_schema else "anyOf"
        # Pydantic puts null last, independent of its position in Union's args.
        ordered = choices + ([type(None)] if len(choices) != len(arguments) else [])
        options = json_schema.get(key, [])
        children = [
            _field_schema(
                choice,
                accordion=accordion,
                json_schema=options[index] if index < len(options) else None,
            )
            for index, choice in enumerate(ordered)
        ]
        return {key: children} if any(children) else {}
    if origin in (list, set, frozenset, Sequence):
        child = (
            _field_schema(
                arguments[0], accordion=False, json_schema=json_schema.get("items")
            )
            if arguments
            else {}
        )
        return {"items": child} if child else {}
    if origin is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            child = _field_schema(
                arguments[0], accordion=False, json_schema=json_schema.get("items")
            )
            return {"items": child} if child else {}
        items = json_schema.get("prefixItems", [])
        children = [
            _field_schema(
                child,
                accordion=False,
                json_schema=items[index] if index < len(items) else None,
            )
            for index, child in enumerate(arguments)
        ]
        return {"items": children} if any(children) else {}
    if origin in (dict, Mapping):
        child = (
            _field_schema(
                arguments[1], json_schema=json_schema.get("additionalProperties")
            )
            if len(arguments) == 2
            else {}
        )
        return {"additionalProperties": child} if child else {}
    if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
        return {}

    # Model's package imports the decorator, which imports this module. Keep the
    # dependency lazy so directly importing the public generator remains safe.
    from simstack.models.document import Model

    is_document = issubclass(annotation, Model)
    if annotation in _generating_models.get():
        if is_document:
            return _model_reference(annotation, accordion=accordion)
        # Embedded values have no database mapping. A finite, root-local UI
        # definition is reused lazily for each actual nested value in the form.
        definition = f"{annotation.__module__}.{annotation.__qualname__}"
        definitions = _embedded_definitions.get()
        if definitions is None:
            raise RuntimeError("Recursive UI generation has no definition registry")
        definitions.setdefault(definition, None)
        return {
            "ui:field": "RecursiveFormField",
            "ui:options": {"definition": definition},
        }
    schema = resolve_ui_schema(annotation)
    if schema is None:
        return {}
    if "ui:field" in schema:
        return deepcopy(schema)
    if is_document:
        return _model_reference(annotation, accordion=accordion)
    # Plain embedded Pydantic values have no independently registered mapping.
    return schema


def generate_ui_schema(cls: type[BaseModel]) -> dict[str, Any]:
    """Generate UI defaults while preserving model and per-field overrides.

    Nested model components are discovered through their own presentation hooks,
    not through workflow names or special cases for built-in model classes.
    """
    base_method = getattr(cls, "ui_base_schema", None)
    base_schema = base_method() if callable(base_method) else {}
    if not isinstance(base_schema, dict):
        raise ValueError("UI base schema must be a JSON object")
    base_schema = deepcopy(base_schema)

    definitions = _embedded_definitions.get()
    definitions_token = None
    if definitions is None:
        definitions = {}
        definitions_token = _embedded_definitions.set(definitions)
    token = _generating_models.set(_generating_models.get() | {cls})
    try:
        # A custom model-level component owns rendering its complete value.
        if "ui:field" not in base_schema:
            json_schema = cls.model_json_schema()
            root = json_schema
            if "$ref" in root and root["$ref"].startswith("#/$defs/"):
                root = json_schema.get("$defs", {}).get(
                    root["$ref"].removeprefix("#/$defs/"), {}
                )
            properties = root.get("properties", {})
            for field_name, field in cls.model_fields.items():
                alias = field.validation_alias
                if isinstance(alias, AliasChoices):
                    alias = next(
                        (
                            path[0]
                            for path in alias.convert_to_aliases()
                            if len(path) == 1 and isinstance(path[0], str)
                        ),
                        None,
                    )
                name = alias if isinstance(alias, str) else field.alias or field_name
                if field_name == "id" and field.serialization_alias == "id":
                    name = "id"
                if name not in base_schema:
                    field_schema = _field_schema(
                        field.annotation, json_schema=properties.get(name)
                    )
                    if field_schema:
                        base_schema[name] = field_schema
        if "id" in cls.model_fields and "id" not in base_schema:
            base_schema["id"] = {"ui:widget": "hidden"}
        definition = f"{cls.__module__}.{cls.__qualname__}"
        if definition in definitions:
            definitions[definition] = deepcopy(base_schema)
        if definitions_token is not None and definitions:
            existing = base_schema.get("ui:definitions", {})
            if not isinstance(existing, dict):
                raise ValueError("UI definitions must be a JSON object")
            if any(schema is None for schema in definitions.values()):
                raise RuntimeError("A recursive UI definition was not generated")
            base_schema["ui:definitions"] = {**definitions, **existing}
        return base_schema
    finally:
        _generating_models.reset(token)
        if definitions_token is not None:
            _embedded_definitions.reset(definitions_token)
