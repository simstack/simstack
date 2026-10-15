from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from simstack.models.document import Model, ObjectId

T = TypeVar("T")


def model_copy(
    model: T,
    *,
    deep: bool = True,
    preserve_ids: bool = False,
    copy_references: bool = False,
) -> T:
    """
    Copy a Pydantic model tree while preserving document-reference semantics.

    Unlike a generic deep copy, nested document models retain their identity
    unless ``copy_references`` is requested. Embedded values and containers are
    isolated without re-running validators or losing fields-set/private state.

    Args:
        model:
            The object to copy.
        deep:
            If True, recursively copy nested containers and embedded models.
            If False, copy the root while retaining its nested values.
        preserve_ids:
            If True, preserve ``id`` fields on copied document models.
            If False, assign fresh ObjectIds to copied top-level/referenced Models.
            Models with custom primary keys require preserve_ids=True.
        copy_references:
            If True, recursively clone nested document models.
            If False, keep nested document references as-is.

            Referenced documents usually represent an existing identity rather
            than a value owned by their containing model.

    Returns:
        A copy with valid Pydantic state and shared-reference relationships.
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
        # Nested documents are copied only if copy_references=True.
        # Otherwise they remain references to the existing domain identity.
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
    # Start from Pydantic's shallow copy to preserve validated field names,
    # extras, private attributes and fields-set state without running validators.
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
        if field_name not in model_values:
            if field_info.is_required():
                raise ValueError(
                    f"Cannot copy {type(source).__name__}: missing required field {field_name!r}"
                )
            model_values[field_name] = field_info.get_default(call_default_factory=True)

    if isinstance(source, Model) and not preserve_ids:
        # Domain documents use generated ObjectIds. An overridden identity type
        # must be retained explicitly rather than replaced by an invalid value.
        if not isinstance(source.id, ObjectId):
            raise ValueError("Use preserve_ids=True for a custom document identity")
        model_values["id"] = ObjectId()

    return copied
