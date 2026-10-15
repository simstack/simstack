"""Pydantic field helpers for SimStack domain models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field as PydanticField
from pydantic.fields import FieldInfo


REFERENCE_SCHEMA_KEY = "x-simstack-reference"
REFERENCE_MODEL_SCHEMA_KEY = "x-simstack-reference-model"
REFERENCE_COLLECTION_SCHEMA_KEY = "x-simstack-reference-collection"


def _merge_schema_extra(
    existing: Any,
    additions: Mapping[str, Any],
) -> dict[str, Any]:
    if existing is None:
        result: dict[str, Any] = {}
    elif isinstance(existing, Mapping):
        result = dict(existing)
    else:
        raise TypeError("json_schema_extra must be a mapping")
    result.update(additions)
    return result


Field = PydanticField


def Reference(  # noqa: N802 - public model declaration API
    default: Any = ...,
    **kwargs: Any,
) -> FieldInfo:
    """Mark a Pydantic field as a separately persisted document reference."""

    kwargs["json_schema_extra"] = _merge_schema_extra(
        kwargs.get("json_schema_extra"), {REFERENCE_SCHEMA_KEY: True}
    )
    return PydanticField(default, **kwargs)


__all__ = [
    "Field",
    "REFERENCE_COLLECTION_SCHEMA_KEY",
    "REFERENCE_MODEL_SCHEMA_KEY",
    "REFERENCE_SCHEMA_KEY",
    "Reference",
]
