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
            If False, copy the root while retaining its nested values.
        preserve_ids:
            If True, preserve ODMantic ``id`` fields on copied ``Model`` objects.
            If False, assign fresh ObjectIds to copied top-level/referenced Models.
            Models with custom primary keys require preserve_ids=True.
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

    if not deep and not _is_root:
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

        copied = _reconstruct_pydantic_model(
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
        # A tuple may have been reached recursively through one of its lists.
        if value_id in _seen:
            return _seen[value_id]
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


def _reconstruct_pydantic_model(
    source: BaseModel,
    *,
    deep: bool,
    preserve_ids: bool,
    copy_references: bool,
    _seen: dict[int, Any],
) -> BaseModel:
    # Pydantic's shallow copy preserves validated field names, extras, private
    # attributes and fields-set state without re-running validators. ODMantic's
    # model_copy recursively marks embedded objects, including shared originals,
    # so initialize bookkeeping only on the copies owned by this traversal.
    copied = BaseModel.__copy__(source)
    _seen[id(source)] = copied

    def copy_value(value: Any) -> Any:
        return _copy_value(
            value,
            deep=deep,
            preserve_ids=preserve_ids,
            copy_references=copy_references,
            _seen=_seen,
        )

    if deep:
        object.__setattr__(copied, "__dict__", copy_value(source.__dict__))
        object.__setattr__(
            copied, "__pydantic_extra__", copy_value(source.__pydantic_extra__)
        )
        object.__setattr__(
            copied, "__pydantic_private__", copy_value(copied.__pydantic_private__)
        )

    model_fields = type(source).model_fields
    model_values = copied.__dict__
    for field_name, field_info in model_fields.items():
        field_value = model_values.get(field_name)
        if field_name not in model_values or _is_odmantic_field_proxy(field_value):
            if field_info.is_required():
                raise ValueError(
                    f"Cannot copy {type(source).__name__}: missing required field {field_name!r}"
                )
            model_values[field_name] = field_info.get_default(call_default_factory=True)

    if isinstance(source, Model) and not preserve_ids:
        # Standard ODMantic Models use a generated ObjectId. Other primary-key
        # types must be retained explicitly rather than replaced by invalid data.
        if source.__primary_field__ != "id" or not isinstance(source.id, ObjectId):
            raise ValueError("Use preserve_ids=True for a custom ODMantic primary key")
        model_values["id"] = ObjectId()

    if isinstance(copied, (Model, EmbeddedModel)):
        object.__setattr__(copied, "__fields_modified__", set(model_fields))

    return copied


def _is_odmantic_field_proxy(value: Any) -> bool:
    value_type = type(value)
    return value_type.__name__ == "FieldProxy" and value_type.__module__.startswith(
        "odmantic"
    )
