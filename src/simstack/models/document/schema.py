"""JSON-schema helpers for persistence-neutral domain models."""

from __future__ import annotations

import re
from types import UnionType
from typing import Any, Union, get_args, get_origin

from .fields import (
    PAYLOAD_SCHEMA_KEY,
    REFERENCE_COLLECTION_SCHEMA_KEY,
    REFERENCE_MODEL_SCHEMA_KEY,
    REFERENCE_SCHEMA_KEY,
)


def default_collection_name(model_name: str) -> str:
    """Return the stable collection name associated with a document type.

    Collection naming is declarative model metadata.  Selecting and accessing
    a physical collection remains the persistence adapter's responsibility.
    """

    if model_name.endswith("Model"):
        model_name = model_name.removesuffix("Model")
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", model_name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first).lower()


def _annotate_reference_schema(annotation: Any, schema: dict[str, Any], *, field_path: str) -> None:
    """Mark document leaves, not an array/map that happens to contain them."""
    for key in (REFERENCE_SCHEMA_KEY, REFERENCE_MODEL_SCHEMA_KEY, REFERENCE_COLLECTION_SCHEMA_KEY):
        schema.pop(key, None)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (Union, UnionType):
        candidates = [item for item in arguments if item is not type(None)]
        if len(candidates) != 1:
            raise TypeError(f"{field_path} must reference exactly one document type per union")
        target = candidates[0]
        branches = [item for item in schema.get("anyOf", [])
                    if isinstance(item, dict) and item.get("type") != "null"]
        if len(branches) != 1:
            raise TypeError(f"{field_path} has no unambiguous nullable reference schema")
        # A nullable scalar still denotes one reference. Preserve its public
        # field-level marker while leaving the schema's null branch untouched.
        if isinstance(target, type) and hasattr(target, "__collection__"):
            _annotate_reference_schema(target, schema, field_path=field_path)
            _annotate_reference_schema(target, branches[0], field_path=field_path)
            return
        _annotate_reference_schema(target, branches[0], field_path=field_path)
        return
    if isinstance(annotation, type) and hasattr(annotation, "__collection__"):
        schema[REFERENCE_SCHEMA_KEY] = True
        schema[REFERENCE_MODEL_SCHEMA_KEY] = annotation.__name__
        schema[REFERENCE_COLLECTION_SCHEMA_KEY] = annotation.__collection__
        return
    if origin in (list, set, frozenset):
        child = schema.get("items")
        if len(arguments) != 1 or not isinstance(child, dict):
            raise TypeError(f"{field_path} has an invalid reference array schema")
        _annotate_reference_schema(arguments[0], child, field_path=f"{field_path}[]")
        return
    if origin is dict:
        child = schema.get("additionalProperties")
        if len(arguments) != 2 or not isinstance(child, dict):
            raise TypeError(f"{field_path} has an invalid reference map schema")
        _annotate_reference_schema(arguments[1], child, field_path=f"{field_path}{{}}")
        return
    if origin is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            child = schema.get("items")
            if not isinstance(child, dict):
                raise TypeError(f"{field_path} has an invalid reference tuple schema")
            _annotate_reference_schema(arguments[0], child, field_path=f"{field_path}[]")
            return
        children = schema.get("prefixItems")
        if not isinstance(children, list) or len(children) != len(arguments):
            raise TypeError(f"{field_path} has an invalid fixed reference tuple schema")
        for index, (target, child) in enumerate(zip(arguments, children)):
            if not isinstance(child, dict):
                raise TypeError(f"{field_path} has an invalid reference tuple item")
            _annotate_reference_schema(target, child, field_path=f"{field_path}[{index}]")
        return
    raise TypeError(f"{field_path} must reference a document type")


def annotate_document_schema(
    model_class: type,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Describe actual reference targets independently from decorative titles."""

    dataset_kind = getattr(model_class, "__dataset_kind__", None)
    if dataset_kind and "sections" in model_class.model_fields:
        schema["x-simstack-dataset"] = {"version": 1, "kind": dataset_kind}
    selection_kind = getattr(model_class, "__dataset_selection_kind__", None)
    if selection_kind:
        schema["x-simstack-dataset-selection"] = {"version": 1, "kind": selection_kind}

    payload_fields = getattr(model_class, "__payload_fields__", ())
    if payload_fields:
        schema[PAYLOAD_SCHEMA_KEY] = {
            "version": 1,
            "fields": [dict(field) for field in payload_fields],
        }

    properties = schema.get("properties")
    if isinstance(properties, dict):
        identity = model_class.model_fields.get("id")
        if identity is not None and identity.alias == "_id" and identity.serialization_alias == "id":
            if "_id" in properties:
                properties["id"] = properties.pop("_id")
            if "_id" in schema.get("required", []):
                schema["required"] = ["id" if name == "_id" else name for name in schema["required"]]
        for field_name, field in model_class.model_fields.items():
            if not (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                continue
            field_schema = properties.get(field.alias or field_name)
            if isinstance(field_schema, dict):
                _annotate_reference_schema(
                    field.annotation, field_schema,
                    field_path=f"{model_class.__name__}.{field_name}",
                )

    target_model: type | None = None
    for base in getattr(model_class, "__orig_bases__", ()):
        origin = get_origin(base)
        if getattr(origin, "__name__", None) != "ObjectListMixin":
            continue
        arguments = get_args(base)
        if len(arguments) == 1 and isinstance(arguments[0], type):
            target_model = arguments[0]
            break

    if target_model is None:
        return schema

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    elements = properties.get("elements")
    if not isinstance(elements, dict):
        return schema
    items = elements.get("items")
    if not isinstance(items, dict):
        return schema

    target_name = target_model.__name__
    items[REFERENCE_SCHEMA_KEY] = True
    items[REFERENCE_MODEL_SCHEMA_KEY] = target_name
    items[REFERENCE_COLLECTION_SCHEMA_KEY] = getattr(
        target_model,
        "__collection__",
        default_collection_name(target_name),
    )
    items.setdefault("title", f"{target_name} reference")
    return schema


__all__ = ["annotate_document_schema", "default_collection_name"]
