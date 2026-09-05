from __future__ import annotations

from typing import Any, TypeVar

from odmantic import EmbeddedModel, Model, ObjectId
from pydantic import BaseModel

T = TypeVar("T")


def model_copy(
    model: T,
    *,
    deep: bool = True,
    preserve_ids: bool = False,
    copy_references: bool = False,
) -> T:
    """
    Safely copy an ODMantic/Pydantic model tree.

    This avoids using ``model.model_copy(deep=True)`` directly on ODMantic models,
    because deep-copying ODMantic ``Model`` references can produce objects with
    broken ODMantic internals, e.g. missing ``__fields_modified__``.

    Args:
        model:
            The object to copy.
        deep:
            If True, recursively copy nested containers and embedded models.
            If False, perform a shallow model reconstruction.
        preserve_ids:
            If True, preserve ODMantic ``id`` fields on copied ``Model`` objects.
            If False, assign fresh ObjectIds to copied top-level/referenced Models.
        copy_references:
            If True, recursively clone nested ODMantic ``Model`` instances.
            If False, keep nested ODMantic ``Model`` references as-is.

            For ODMantic ``Reference()`` fields this is often the safest default,
            because references usually represent database identity.

    Returns:
        A reconstructed copy with valid Pydantic/ODMantic internals.
    """
    return _copy_value(
        model,
        deep=deep,
        preserve_ids=preserve_ids,
        copy_references=copy_references,
        _seen={},
        _is_root=True,
    )


def _copy_value(
    value: Any,
    *,
    deep: bool,
    preserve_ids: bool,
    copy_references: bool,
    _seen: dict[int, Any],
    _is_root: bool = False,
) -> Any:
    if value is None:
        return None

    if not deep:
        return value

    value_id = id(value)
    if value_id in _seen:
        return _seen[value_id]

    if isinstance(value, Model):
        # Root model is always copied.
        # Nested ODMantic Models are copied only if copy_references=True.
        # Otherwise they remain as references to the original DB object.
        if not _is_root and not copy_references:
            return value

        copied = _reconstruct_odmantic_model(
            value,
            deep=deep,
            preserve_ids=preserve_ids,
            copy_references=copy_references,
            _seen=_seen,
        )
        return copied

    if isinstance(value, EmbeddedModel):
        copied = _reconstruct_pydantic_model(
            value,
            deep=deep,
            preserve_ids=preserve_ids,
            copy_references=copy_references,
            _seen=_seen,
        )
        return copied

    if isinstance(value, BaseModel):
        copied = _reconstruct_pydantic_model(
            value,
            deep=deep,
            preserve_ids=preserve_ids,
            copy_references=copy_references,
            _seen=_seen,
        )
        return copied

    if isinstance(value, list):
        copied_list: list[Any] = []
        _seen[value_id] = copied_list
        copied_list.extend(
            _copy_value(
                item,
                deep=deep,
                preserve_ids=preserve_ids,
                copy_references=copy_references,
                _seen=_seen,
            )
            for item in value
        )
        return copied_list

    if isinstance(value, tuple):
        copied_tuple = tuple(
            _copy_value(
                item,
                deep=deep,
                preserve_ids=preserve_ids,
                copy_references=copy_references,
                _seen=_seen,
            )
            for item in value
        )
        _seen[value_id] = copied_tuple
        return copied_tuple

    if isinstance(value, set):
        copied_set: set[Any] = set()
        _seen[value_id] = copied_set
        copied_set.update(
            _copy_value(
                item,
                deep=deep,
                preserve_ids=preserve_ids,
                copy_references=copy_references,
                _seen=_seen,
            )
            for item in value
        )
        return copied_set

    if isinstance(value, dict):
        copied_dict: dict[Any, Any] = {}
        _seen[value_id] = copied_dict
        for key, item in value.items():
            copied_key = _copy_value(
                key,
                deep=deep,
                preserve_ids=preserve_ids,
                copy_references=copy_references,
                _seen=_seen,
            )
            copied_item = _copy_value(
                item,
                deep=deep,
                preserve_ids=preserve_ids,
                copy_references=copy_references,
                _seen=_seen,
            )
            copied_dict[copied_key] = copied_item
        return copied_dict

    return value


def _reconstruct_odmantic_model(
    source: Model,
    *,
    deep: bool,
    preserve_ids: bool,
    copy_references: bool,
    _seen: dict[int, Any],
) -> Model:
    model_cls = type(source)

    payload: dict[str, Any] = {}
    source_values = object.__getattribute__(source, "__dict__")

    for field_name, field_info in model_cls.model_fields.items():
        if field_name == "id":
            continue

        if field_name in source_values:
            field_value = source_values[field_name]
        else:
            field_value = _field_default(field_info)

        payload[field_name] = _copy_value(
            field_value,
            deep=deep,
            preserve_ids=preserve_ids,
            copy_references=copy_references,
            _seen=_seen,
        )

    copied = model_cls.model_validate(payload)
    _seen[id(source)] = copied

    if preserve_ids:
        object.__setattr__(copied, "id", source.id)
    else:
        object.__setattr__(copied, "id", ObjectId())

    _restore_missing_defaults(copied)
    _sanitize_odmantic_copy(copied)
    return copied


def _reconstruct_pydantic_model(
    source: BaseModel,
    *,
    deep: bool,
    preserve_ids: bool,
    copy_references: bool,
    _seen: dict[int, Any],
) -> BaseModel:
    model_cls = type(source)

    payload: dict[str, Any] = {}
    source_values = object.__getattribute__(source, "__dict__")

    for field_name, field_info in model_cls.model_fields.items():
        if field_name in source_values:
            field_value = source_values[field_name]
        else:
            field_value = _field_default(field_info)

        payload[field_name] = _copy_value(
            field_value,
            deep=deep,
            preserve_ids=preserve_ids,
            copy_references=copy_references,
            _seen=_seen,
        )

    copied = model_cls.model_validate(payload)
    _seen[id(source)] = copied

    _restore_missing_defaults(copied)
    if isinstance(copied, Model):
        _sanitize_odmantic_copy(copied)

    return copied


def _field_default(field_info: Any) -> Any:
    """
    Return a field default, including explicit None defaults for Optional fields.

    Pydantic/ODMantic models can behave badly if a non-required field is missing
    from __dict__ but appears in ODMantic's modified-field bookkeeping.
    """
    if hasattr(field_info, "is_required") and field_info.is_required():
        return None

    if hasattr(field_info, "get_default"):
        try:
            return field_info.get_default(call_default_factory=True)
        except TypeError:
            return field_info.get_default()

    return None


def _restore_missing_defaults(value: Any, *, _seen: set[int] | None = None) -> Any:
    """
    Ensure every declared Pydantic/ODMantic field exists in __dict__.

    This is important for Optional[float], Optional[str], and similar fields with
    default=None. ODMantic may later try to dump a field because it is marked as
    modified; if the field is absent from __dict__, model_dump_doc can fail with
    a KeyError at raw_doc[field_name].
    """
    if not isinstance(value, BaseModel):
        return value

    if _seen is None:
        _seen = set()

    value_id = id(value)
    if value_id in _seen:
        return value
    _seen.add(value_id)

    model_values = object.__getattribute__(value, "__dict__")
    model_cls = type(value)
    model_fields = model_cls.model_fields

    # Primary pass: Ensure all declared fields are in __dict__
    for field_name, field_info in model_fields.items():
        if field_name == "id":
            continue

        if field_name not in model_values or _is_odmantic_field_proxy(model_values[field_name]):
            object.__setattr__(value, field_name, _field_default(field_info))

    # Second pass: Recursively restore defaults in nested models
    # We use a static set of keys to avoid issues if __dict__ changes during iteration
    for field_name in list(model_values.keys()):
        field_value = model_values[field_name]
        if isinstance(field_value, (BaseModel, Model)):
            _restore_missing_defaults(field_value, _seen=_seen)
        elif isinstance(field_value, list):
            for item in field_value:
                if isinstance(item, (BaseModel, Model)):
                    _restore_missing_defaults(item, _seen=_seen)
        elif isinstance(field_value, tuple):
            for item in field_value:
                if isinstance(item, (BaseModel, Model)):
                    _restore_missing_defaults(item, _seen=_seen)
        elif isinstance(field_value, dict):
            for item in field_value.values():
                if isinstance(item, (BaseModel, Model)):
                    _restore_missing_defaults(item, _seen=_seen)

    return value


def _is_odmantic_field_proxy(value: Any) -> bool:
    value_type = type(value)
    return (
        value_type.__name__ == "FieldProxy"
        and value_type.__module__.startswith("odmantic")
    )


def _sanitize_odmantic_copy(value: Any) -> Any:
    """
    Normalize ODMantic internal modified-field state after reconstructing a model.

    This keeps the copied model saveable by ODMantic and avoids stale modified-field
    entries that can appear after manual reconstruction or mutation.
    """
    if not isinstance(value, Model):
        return value

    _restore_missing_defaults(value)

    modified = getattr(value, "__fields_modified__", None)
    model_values = getattr(value, "__dict__", None)

    if isinstance(modified, set) and isinstance(model_values, dict):
        valid_fields = set(model_values)
        valid_fields.add("id")

        sanitized: set[str] = set()
        for field_name in modified:
            if field_name not in valid_fields:
                if field_name in value.model_fields:
                    # Restore default for missing field that is marked as modified
                    field_info = value.model_fields[field_name]
                    object.__setattr__(value, field_name, _field_default(field_info))
                else:
                    continue
            
            try:
                # Validate that we can actually dump this field
                value.model_dump_doc(include={field_name})
            except Exception:
                continue
            sanitized.add(field_name)

        object.__setattr__(value, "__fields_modified__", sanitized)

    return value
