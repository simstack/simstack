"""Pydantic field declarations with passive domain schema metadata."""

from __future__ import annotations

from typing import Any

from pydantic import Field as PydanticField
from pydantic.fields import FieldInfo

REFERENCE_SCHEMA_KEY = "x-simstack-reference"
REFERENCE_MODEL_SCHEMA_KEY = "x-simstack-reference-model"
REFERENCE_COLLECTION_SCHEMA_KEY = "x-simstack-reference-collection"
PAYLOAD_SCHEMA_KEY = "x-simstack-payload"
INDEX_SCHEMA_KEY = "x-simstack-index"
PRIMARY_SCHEMA_KEY = "x-simstack-primary"


def Field(
    default: Any = ...,
    *,
    key_name: str | None = None,
    primary_field: bool = False,
    index: bool = False,
    unique: bool = False,
    **kwargs: Any,
) -> FieldInfo:
    """Keep declaration compatibility; Server alone interprets index metadata."""
    extra = kwargs.get("json_schema_extra")
    additions = {}
    if index or unique:
        additions[INDEX_SCHEMA_KEY] = {"unique": bool(unique)}
    if primary_field:
        additions[PRIMARY_SCHEMA_KEY] = True
    if key_name is not None:
        kwargs.setdefault("alias", key_name)
    if additions:
        if extra is not None and not isinstance(extra, dict):
            raise TypeError(
                "Indexed/reference fields require mapping json_schema_extra"
            )
        kwargs["json_schema_extra"] = {**(extra or {}), **additions}
    return PydanticField(default, **kwargs)


def Reference(
    default: Any = ..., *, key_name: str | None = None, **kwargs: Any
) -> FieldInfo:
    """Declare a reference. Embedded model values still remain embedded."""
    extra = kwargs.get("json_schema_extra")
    if extra is not None and not isinstance(extra, dict):
        raise TypeError("Reference json_schema_extra must be a mapping")
    kwargs["json_schema_extra"] = {**(extra or {}), REFERENCE_SCHEMA_KEY: True}
    return Field(default, key_name=key_name, **kwargs)


__all__ = [
    "Field",
    "Reference",
    "REFERENCE_SCHEMA_KEY",
    "REFERENCE_MODEL_SCHEMA_KEY",
    "REFERENCE_COLLECTION_SCHEMA_KEY",
    "PAYLOAD_SCHEMA_KEY",
    "INDEX_SCHEMA_KEY",
    "PRIMARY_SCHEMA_KEY",
]
