"""Deterministic identity for a loaded node implementation, independent of source paths."""
from __future__ import annotations

import hashlib
import functools
import inspect
import json
import sys
from enum import Enum
from pathlib import PurePath
from types import CodeType, FunctionType
from typing import Any, Callable

from pydantic import BaseModel


def _qualified_name(value: Any) -> str:
    return f"{value.__module__}.{value.__qualname__}"


def _encode(value: Any, seen: dict[int, tuple[int, Any]], declared_version: str | None) -> Any:
    if value is None or value is Ellipsis:
        return [type(value).__name__]
    if isinstance(value, Enum):
        return ["enum", _qualified_name(type(value)), _encode(value.value, seen, declared_version)]
    if isinstance(value, (bool, int, float, complex, str, bytes)):
        return [type(value).__name__, value.hex() if isinstance(value, bytes) else repr(value)]
    if isinstance(value, PurePath):
        return ["path", str(value)]
    if isinstance(value, type):
        return ["type", _qualified_name(value)]
    if id(value) in seen:
        return ["reference", seen[id(value)][0]]
    # Keep strong references as well as deterministic indexes: temporary
    # normalized containers must not have their ids reused during traversal.
    seen[id(value)] = (len(seen), value)
    if isinstance(value, CodeType):
        constants = list(value.co_consts)
        if value.co_flags & inspect.CO_NEWLOCALS and constants and isinstance(constants[0], str):
            constants[0] = None  # A function's docstring does not change its execution.
        return ["code", value.co_code.hex(), value.co_exceptiontable.hex(),
                value.co_argcount, value.co_posonlyargcount, value.co_kwonlyargcount,
                value.co_flags, value.co_names, value.co_varnames, value.co_freevars,
                value.co_cellvars, _encode(constants, seen, declared_version)]
    if isinstance(value, FunctionType):
        closure = []
        for cell in value.__closure__ or ():
            try:
                closure.append(cell.cell_contents)
            except ValueError:
                closure.append(Ellipsis)
        return ["function", _qualified_name(value),
                _encode(value.__code__, seen, declared_version),
                _encode(value.__defaults__, seen, declared_version),
                _encode(value.__kwdefaults__, seen, declared_version),
                _encode(closure, seen, declared_version)]
    if isinstance(value, (list, tuple, set, frozenset)):
        ordered = sorted(value, key=lambda item: json.dumps(
            _encode(item, dict(seen), declared_version), sort_keys=True
        )) if isinstance(value, (set, frozenset)) else value
        items = [_encode(item, seen, declared_version) for item in ordered]
        return [type(value).__name__, items]
    if isinstance(value, dict):
        items = [[_encode(key, seen, declared_version), _encode(item, seen, declared_version)]
                 for key, item in value.items()]
        return ["dict", items]
    if isinstance(value, functools.partial):
        return ["partial", _encode(value.func, seen, declared_version),
                _encode(value.args, seen, declared_version),
                _encode(value.keywords, seen, declared_version)]
    if isinstance(value, BaseModel):
        return ["model", _qualified_name(type(value)),
                _encode(value.model_dump(exclude={"id"}), seen, declared_version)]
    if declared_version is not None:
        # Opaque dependency state belongs to the explicitly declared version.
        return ["external", _qualified_name(type(value))]
    raise TypeError(
        f"Cannot derive a node code version for captured/default value {_qualified_name(type(value))}; "
        "set @node(version=...) to version this dependency explicitly"
    )


def compute_node_code_version(func: Callable[..., Any]) -> str:
    """Fingerprint code, defaults, captured values and the declared node version.

    Filename, source formatting and line tables are excluded. External globals
    and dependencies are deliberately not traversed: bump ``@node(version=...)``
    when their behavior changes without changing this implementation.
    """
    declared_version = getattr(func, "_node_version", None)
    payload = ["simstack-node-code-v1", sys.implementation.cache_tag, declared_version,
               _encode(func, {}, declared_version)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def node_code_version(func: Callable[..., Any]) -> str:
    """Keep the declaration's identity stable while captured runtime state changes."""
    declared_version = getattr(func, "_node_version", None)
    defaults = _encode((func.__defaults__, func.__kwdefaults__), {}, declared_version)
    cache_key = (func.__code__, declared_version, defaults)
    cached = getattr(func, "_node_code_version", None)
    if cached is None or cached[0] != cache_key:
        cached = (cache_key, compute_node_code_version(func))
        setattr(func, "_node_code_version", cached)
    return cached[1]
