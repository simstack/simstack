"""Pydantic field declarations with passive domain schema metadata."""

from __future__ import annotations

from typing import Any

from pydantic import Field as PydanticField
from pydantic.fields import FieldInfo

REFERENCE_SCHEMA_KEY = "x-simstack-reference"
REFERENCE_MODEL_SCHEMA_KEY = "x-simstack-reference-model"
PAYLOAD_SCHEMA_KEY = "x-simstack-payload"
UNIQUE_SCHEMA_KEY = "x-simstack-unique"
PRIMARY_SCHEMA_KEY = "x-simstack-primary"


def Field(
    default: Any = ...,
    *,
    key_name: str | None = None,
    primary_field: bool = False,
    unique: bool = False,
    **kwargs: Any,
) -> FieldInfo:
    """Declare scientific field aliases, identity and domain uniqueness."""
    if "index" in kwargs:
        raise TypeError("Database indexes are declared by Server storage")
    extra = kwargs.get("json_schema_extra")
    additions = {}
    if unique:
        additions[UNIQUE_SCHEMA_KEY] = True
    if primary_field:
        additions[PRIMARY_SCHEMA_KEY] = True
    if key_name is not None:
        kwargs.setdefault("alias", key_name)
    if additions:
        if extra is not None and not isinstance(extra, dict):
            raise TypeError(
                "Identity and unique fields require mapping json_schema_extra"
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
    "PAYLOAD_SCHEMA_KEY",
    "UNIQUE_SCHEMA_KEY",
    "PRIMARY_SCHEMA_KEY",
]
