"""Stable domain input data, independent of persistence bookkeeping."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def hashable_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return hashable_inputs(value)
    if isinstance(value, list):
        return [hashable_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(hashable_value(item) for item in value)
    if isinstance(value, dict):
        return {key: hashable_value(item) for key, item in value.items()}
    return value


def model_field_values(model: BaseModel, *, exclude_identity: bool = True) -> dict[str, Any]:
    """Return declared values and intentional extras without runtime internals."""
    semantic_inputs = getattr(model, "__simstack_hash_inputs__", None)
    if callable(semantic_inputs):
        fields = dict(semantic_inputs())
        if not exclude_identity and "id" in type(model).model_fields:
            fields["id"] = model.id
        return fields
    fields = {
        name: getattr(model, name)
        for name in type(model).model_fields
        if not exclude_identity or name != "id"
    }
    fields.update(model.model_extra or {})
    return fields


def hashable_inputs(model: BaseModel) -> dict[str, Any]:
    """Hash declared fields and intentional extras, never runtime internals."""
    fields = model_field_values(model)
    return {name: hashable_value(value) for name, value in fields.items()}
