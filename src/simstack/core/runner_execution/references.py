"""Typed embedded-reference traversal and iterative runtime graph walking."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from simstack.models.document import REFERENCE_SCHEMA_KEY, Model
from simstack.models.document.reference_values import transform_reference_values

from .errors import RunnerTaskPayloadError


def _matches_model_shape(value: Mapping[str, Any], model: type[BaseModel]) -> bool:
    aliases = {field.alias or name for name, field in model.model_fields.items()}
    names = set(model.model_fields)
    if model.model_config.get("extra") != "allow" and set(value) - aliases - names:
        return False
    for name, field in model.model_fields.items():
        key = field.alias or name
        if key not in value and name not in value:
            if field.is_required():
                return False
            continue
        if get_origin(field.annotation) is Literal and value.get(key, value.get(name)) not in get_args(field.annotation):
            return False
    return True


def _contains_reference(annotation: Any) -> bool:
    pending = [annotation]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop()
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        pending.extend(get_args(candidate))
        if inspect.isclass(candidate) and issubclass(candidate, BaseModel):
            for field in candidate.model_fields.values():
                if (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                    return True
                pending.append(field.annotation)
    return False


def transform_model_references(
    value: Any,
    annotation: Any,
    resolve: Callable[[Any, type[Model], str], Any],
    *,
    path: str,
) -> Any:
    """Transform declared Reference fields, including typed embedded containers.

    Document IDs are handled by the caller's graph; only finite embedded value
    structures recurse here. Ambiguous unions are rejected instead of guessing
    a domain type or relying on repository access.
    """
    if value is None or isinstance(value, BaseModel) or not _contains_reference(annotation):
        return value
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (Union, UnionType):
        candidates = [candidate for candidate in arguments if inspect.isclass(candidate) and issubclass(candidate, BaseModel)]
        matching = [candidate for candidate in candidates if isinstance(value, Mapping) and _matches_model_shape(value, candidate)]
        if len(matching) > 1:
            raise RunnerTaskPayloadError(f"Ambiguous embedded document union at {path}")
        return transform_model_references(value, matching[0], resolve, path=path) if matching else value
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel) and isinstance(value, Mapping):
        prepared = dict(value)
        for name, field in annotation.model_fields.items():
            key = field.alias if field.alias in prepared else name
            if key not in prepared or prepared[key] is None:
                continue
            child_path = f"{path}.{name}"
            if (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                try:
                    prepared[key] = transform_reference_values(
                        prepared[key], field.annotation, resolve, path=child_path,
                    )
                except ValueError as exc:
                    raise RunnerTaskPayloadError(f"Invalid reference at {child_path}: {exc}") from exc
            else:
                prepared[key] = transform_model_references(prepared[key], field.annotation, resolve, path=child_path)
        return prepared
    if origin in (list, set, frozenset, tuple) and arguments and isinstance(value, (list, tuple)):
        annotations = arguments
        if origin is not tuple or (len(arguments) == 2 and arguments[1] is Ellipsis):
            annotations = (arguments[0],) * len(value)
        elif len(arguments) != len(value):
            return value  # Preserve invalid shape for the model's own validator.
        return [transform_model_references(item, item_type, resolve, path=f"{path}[{index}]") for index, (item, item_type) in enumerate(zip(value, annotations))]
    if origin in (dict, Mapping) and len(arguments) == 2 and isinstance(value, Mapping):
        return {key: transform_model_references(item, arguments[1], resolve, path=f"{path}[{key!r}]") for key, item in value.items()}
    return value


def walk_runtime_values(roots: Sequence[Any]):
    """Yield each runtime value once without a recursive model/reference walk."""
    pending = list(reversed(roots))
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        yield value
        children: list[Any] = []
        if isinstance(value, BaseModel):
            fields = object.__getattribute__(value, "__dict__")
            children.extend(fields[name] for name in type(value).model_fields)
            children.extend((getattr(value, "__pydantic_extra__", None) or {}).values())
        elif isinstance(value, Mapping):
            children.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            children.extend(value)
        get_cache = getattr(value, "_get_cache", None)
        if callable(get_cache):
            children.extend(get_cache())
        pending.extend(reversed(children))
