"""Persistence-neutral identifiers for SimStack domain documents."""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from typing import Any

from pydantic_core import core_schema

_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
_OBJECT_ID_PROCESS_RANDOM = secrets.token_bytes(5)
_OBJECT_ID_COUNTER = int.from_bytes(secrets.token_bytes(3), "big")
_OBJECT_ID_COUNTER_LOCK = threading.Lock()


def _reset_identifier_generator_after_fork() -> None:
    """A fork must not share its parent's random namespace or acquired lock."""
    global _OBJECT_ID_PROCESS_RANDOM, _OBJECT_ID_COUNTER, _OBJECT_ID_COUNTER_LOCK
    _OBJECT_ID_PROCESS_RANDOM = secrets.token_bytes(5)
    _OBJECT_ID_COUNTER = int.from_bytes(secrets.token_bytes(3), "big")
    _OBJECT_ID_COUNTER_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_identifier_generator_after_fork)


class ObjectId(str):
    """JSON-safe domain identifier with the textual shape of a Mongo ObjectId.

    The type is deliberately independent from BSON.  A persistence adapter may
    translate it at its boundary, while runners only ever see a string value.
    """

    def __new__(cls, value: object | None = None) -> "ObjectId":
        if value is None:
            global _OBJECT_ID_COUNTER

            timestamp = int(time.time()).to_bytes(4, "big", signed=False)
            with _OBJECT_ID_COUNTER_LOCK:
                counter = _OBJECT_ID_COUNTER
                _OBJECT_ID_COUNTER = (_OBJECT_ID_COUNTER + 1) & 0xFFFFFF
            raw = (
                timestamp
                + _OBJECT_ID_PROCESS_RANDOM
                + counter.to_bytes(3, "big", signed=False)
            )
            text = raw.hex()
        elif isinstance(value, bytes):
            if len(value) != 12:
                raise ValueError("ObjectId bytes must be exactly 12 bytes")
            text = value.hex()
        else:
            text = str(value)

        if not _OBJECT_ID_RE.fullmatch(text):
            raise ValueError(f"Invalid ObjectId: {text!r}")
        return str.__new__(cls, text.lower())

    @classmethod
    def is_valid(cls, value: object) -> bool:
        try:
            cls(value)
        except (TypeError, ValueError):
            return False
        return True

    @classmethod
    def __get_pydantic_core_schema__(cls, _source: Any, _handler: Any):
        def validate(value: object) -> "ObjectId":
            if isinstance(value, cls):
                return value
            return cls(value)

        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(
                validate,
                core_schema.str_schema(),
            ),
            python_schema=core_schema.no_info_plain_validator_function(validate),
            serialization=core_schema.plain_serializer_function_ser_schema(
                str,
                return_schema=core_schema.str_schema(),
                when_used="json",
            ),
        )


__all__ = ["ObjectId"]
