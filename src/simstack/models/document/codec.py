"""Lossless JSON values for domain records; independent of any database driver."""

from __future__ import annotations

import base64
import math
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

TYPE_KEY = "__simstack_type__"


class DocumentParsingError(ValueError):
    """A domain document or typed transport value failed validation."""


def _typed(kind, value):
    return {TYPE_KEY: kind, "value": value}


def encode_document_value(value: Any) -> Any:
    """Encode once at an API boundary, retaining scientific binary/numeric data.

    A user mapping containing the reserved discriminator is escaped, so values
    such as metadata dictionaries cannot be mistaken for a typed envelope.
    """
    if isinstance(value, Enum):
        return encode_document_value(value.value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _typed("bytes", base64.b64encode(bytes(value)).decode("ascii"))
    if isinstance(value, datetime):
        return _typed("datetime", value.isoformat())
    if isinstance(value, date):
        return _typed("date", value.isoformat())
    if isinstance(value, time):
        return _typed("time", value.isoformat())
    if isinstance(value, Decimal):
        return _typed("decimal", str(value))
    if isinstance(value, UUID):
        return _typed("uuid", str(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return _typed(
            "float", "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"
        )
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and abs(value) > 2**53 - 1
    ):
        return _typed("int", str(value))
    if isinstance(value, BaseModel):
        return encode_document_value(
            value.model_dump(mode="python", serialize_as_any=True)
        )
    if isinstance(value, Mapping):
        if TYPE_KEY in value:
            return _typed(
                "mapping",
                [
                    [str(key), encode_document_value(item)]
                    for key, item in value.items()
                ],
            )
        return {str(key): encode_document_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode_document_value(item) for item in value]
    return value


def decode_document_value(value: Any) -> Any:
    """Decode a wire value exactly once, validating all reserved envelopes."""
    if isinstance(value, Mapping):
        if TYPE_KEY in value:
            if set(value) != {TYPE_KEY, "value"}:
                raise DocumentParsingError(
                    "Typed domain value cannot contain sibling keys"
                )
            kind, encoded = value[TYPE_KEY], value["value"]
            if kind == "mapping":
                if not isinstance(encoded, list):
                    raise DocumentParsingError(
                        "Escaped mapping must contain key/value pairs"
                    )
                result = {}
                for pair in encoded:
                    if (
                        not isinstance(pair, list)
                        or len(pair) != 2
                        or not isinstance(pair[0], str)
                        or pair[0] in result
                    ):
                        raise DocumentParsingError("Invalid escaped mapping entry")
                    result[pair[0]] = decode_document_value(pair[1])
                return result
            if not isinstance(encoded, str):
                raise DocumentParsingError("Typed domain value must contain a string")
            try:
                if kind == "bytes":
                    return base64.b64decode(encoded.encode("ascii"), validate=True)
                if kind == "datetime":
                    return datetime.fromisoformat(encoded)
                if kind == "date":
                    return date.fromisoformat(encoded)
                if kind == "time":
                    return time.fromisoformat(encoded)
                if kind == "decimal":
                    return Decimal(encoded)
                if kind == "uuid":
                    return UUID(encoded)
                if kind == "int":
                    integer = int(encoded)
                    if str(integer) != encoded:
                        raise ValueError("Integer must be canonical")
                    return integer
                if kind == "float" and encoded in {"nan", "inf", "-inf"}:
                    return float(encoded)
            except (ValueError, TypeError, UnicodeEncodeError, ArithmeticError) as exc:
                raise DocumentParsingError(f"Invalid {kind} domain value") from exc
            raise DocumentParsingError(f"Unknown domain value type: {kind!r}")
        return {str(key): decode_document_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_document_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(decode_document_value(item) for item in value)
    return value


__all__ = [
    "TYPE_KEY",
    "DocumentParsingError",
    "encode_document_value",
    "decode_document_value",
]
