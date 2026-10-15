"""Transform typed document-reference values without any persistence dependency."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from types import UnionType
from typing import Annotated, Any, TYPE_CHECKING, Union, get_args, get_origin

if TYPE_CHECKING:
    from .models import Model


def _document_model(annotation: Any) -> type[Model] | None:
    # Import lazily: this helper can also be consumed by model schema generation.
    from .models import Model

    if inspect.isclass(annotation) and issubclass(annotation, Model):
        return annotation
    return None


def _unwrap(annotation: Any) -> Any:
    while True:
        origin = get_origin(annotation)
        arguments = get_args(annotation)
        if origin is Annotated:
            annotation = arguments[0]
        elif origin in (Union, UnionType):
            non_null = [item for item in arguments if item is not type(None)]
            if len(non_null) != 1:
                raise ValueError("A Reference cannot declare an ambiguous document union")
            annotation = non_null[0]
        else:
            return annotation


def _contains_document(annotation: Any) -> bool:
    annotation = _unwrap(annotation)
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (list, tuple, set, frozenset):
        # Inspect every fixed tuple position, including positions after the first
        # model, so an ambiguous leaf is not hidden by a short-circuiting any().
        contained = [_contains_document(item) for item in arguments if item is not Ellipsis]
        return any(contained)
    if origin in (dict, Mapping) and len(arguments) == 2:
        return _contains_document(arguments[1])
    return _document_model(annotation) is not None


def _plan(value: Any, annotation: Any, path: str):
    annotation = _unwrap(annotation)
    if value is None:
        return "value", value
    target = _document_model(annotation)
    if target is not None:
        return "reference", target
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (list, tuple, set, frozenset) and arguments:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError(f"Reference container at {path} must be a sequence")
        values = list(value)
        if origin is not tuple or (len(arguments) == 2 and arguments[1] is Ellipsis):
            annotations = (arguments[0],) * len(values)
        else:
            if len(arguments) != len(values):
                raise ValueError(f"Reference tuple at {path} has an invalid length")
            annotations = arguments
        return "sequence", [
            (item, item_type, f"{path}[{index}]")
            for index, (item, item_type) in enumerate(zip(values, annotations))
        ]
    if origin in (dict, Mapping) and len(arguments) == 2:
        if not isinstance(value, Mapping):
            raise ValueError(f"Reference container at {path} must be a mapping")
        return "mapping", [
            (key, item, arguments[1], f"{path}[{key!r}]")
            for key, item in value.items()
        ]
    return "value", value


def transform_reference_values(
    value: Any,
    annotation: Any,
    resolve: Callable[[Any, type[Model], str], Any],
    *,
    path: str,
) -> Any:
    """Resolve exact document leaves in scalar/nullable/typed-container references.

    Ordinary primitive tuple positions and mapping keys are retained. No model
    union is guessed and invalid container shapes raise ValueError. The caller
    owns ID validation, document lookup, graph lineage and error translation.
    """
    if not _contains_document(annotation):
        raise ValueError("A Reference must contain a document model")

    def transform(item, item_type, item_path):
        kind, payload = _plan(item, item_type, item_path)
        if kind == "reference":
            return resolve(item, payload, item_path)
        if kind == "sequence":
            transformed = [transform(*child) for child in payload]
            return tuple(transformed) if isinstance(item, tuple) else transformed
        if kind == "mapping":
            return {key: transform(child, child_type, child_path) for key, child, child_type, child_path in payload}
        return payload

    return transform(value, annotation, path)


async def async_transform_reference_values(
    value: Any,
    annotation: Any,
    resolve: Callable[[Any, type[Model], str], Awaitable[Any]],
    *,
    path: str,
) -> Any:
    """Async counterpart sharing the exact annotation and container validation."""
    if not _contains_document(annotation):
        raise ValueError("A Reference must contain a document model")

    async def transform(item, item_type, item_path):
        kind, payload = _plan(item, item_type, item_path)
        if kind == "reference":
            return await resolve(item, payload, item_path)
        if kind == "sequence":
            transformed = [await transform(*child) for child in payload]
            return tuple(transformed) if isinstance(item, tuple) else transformed
        if kind == "mapping":
            return {key: await transform(child, child_type, child_path) for key, child, child_type, child_path in payload}
        return payload

    return await transform(value, annotation, path)


__all__ = ["async_transform_reference_values", "transform_reference_values"]
