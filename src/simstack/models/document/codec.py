"""Lossless JSON wire codecs for domain values.

These functions define a transport/storage representation only.  They neither
know about MongoDB nor perform persistence.
"""

from __future__ import annotations

import base64
import enum
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from .identity import ObjectId


class DocumentParsingError(ValueError):
    """Raised when a wire/storage document cannot be decoded safely."""


def encode_document_value(value: Any) -> Any:
    """Recursively convert a domain value to a lossless JSON-safe value."""

    if isinstance(value, enum.Enum):
        return encode_document_value(value.value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {"$binary": {"base64": encoded, "subType": "00"}}
    if isinstance(value, BaseModel):
        return encode_document_value(value.model_dump(mode="python", by_alias=True))
    if isinstance(value, Mapping):
        return {str(key): encode_document_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode_document_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return {"$date": value.isoformat()}
    if isinstance(value, Path):
        return str(value)
    return value


def decode_document_value(value: Any) -> Any:
    """Reverse :func:`encode_document_value`, including nested binary data."""

    if isinstance(value, Mapping):
        numeric = set(value) & {"$numberLong", "$numberDouble", "$numberDecimal"}
        if numeric:
            if len(numeric) != 1 or len(value) != 1:
                raise DocumentParsingError("Numeric envelope must contain exactly one typed value")
            tag = next(iter(numeric))
            encoded = value[tag]
            if not isinstance(encoded, str):
                raise DocumentParsingError("Numeric envelope value must be a string")
            if tag == "$numberLong":
                if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", encoded) is None or len(encoded) > 20:
                    raise DocumentParsingError("Invalid canonical numberLong value")
                number = int(encoded)
                if not -(2**63) <= number < 2**63:
                    raise DocumentParsingError("numberLong is outside signed 64-bit range")
                return number
            if tag == "$numberDouble":
                if encoded not in {"NaN", "Infinity", "-Infinity"}:
                    raise DocumentParsingError("Unsupported nonfinite numberDouble value")
                return float(encoded)
            try:
                number = Decimal(encoded)
            except Exception as exc:
                raise DocumentParsingError("Invalid numberDecimal value") from exc
            if not number.is_finite() or not -6176 <= number.adjusted() <= 6144:
                raise DocumentParsingError("Unsupported numberDecimal value")
            return number
        if "$binary" in value:
            if set(value) != {"$binary"}:
                raise DocumentParsingError(
                    "$binary envelope cannot contain sibling keys"
                )
            binary = value["$binary"]
            if not isinstance(binary, Mapping):
                raise DocumentParsingError("$binary must contain an object")
            if set(binary) != {"base64", "subType"}:
                raise DocumentParsingError(
                    "$binary must contain exactly base64 and subType"
                )
            encoded = binary.get("base64")
            subtype = binary.get("subType")
            if not isinstance(encoded, str):
                raise DocumentParsingError("$binary.base64 must be a string")
            if subtype != "00":
                raise DocumentParsingError(
                    f"Unsupported binary subtype for domain bytes: {subtype!r}"
                )
            try:
                return base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError) as exc:
                raise DocumentParsingError("Invalid base64 binary value") from exc
        if "$date" in value:
            if set(value) != {"$date"}:
                raise DocumentParsingError("$date envelope cannot contain sibling keys")
            encoded = value["$date"]
            if not isinstance(encoded, str):
                raise DocumentParsingError("$date must contain an ISO string")
            try:
                if "T" in encoded:
                    return datetime.fromisoformat(encoded.replace("Z", "+00:00"))
                return date.fromisoformat(encoded)
            except ValueError as exc:
                raise DocumentParsingError("Invalid ISO date value") from exc
        return {str(key): decode_document_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_document_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(decode_document_value(item) for item in value)
    return value


__all__ = [
    "DocumentParsingError",
    "decode_document_value",
    "encode_document_value",
]
