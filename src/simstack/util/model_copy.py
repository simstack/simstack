from __future__ import annotations

from typing import Any, TypeVar

from simstack.models.document import EmbeddedModel, Model, ObjectId
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
    Copy a Pydantic model tree while preserving declared record identities.

    Args:
        model:
            The object to copy.
        deep:
            If True, recursively copy nested containers and embedded models.
            If False, copy the root while retaining its nested values.
        preserve_ids:
            If True, preserve ``id`` fields on copied ``Model`` objects.
            If False, assign fresh ObjectIds to copied top-level/referenced Models.
            Models with custom primary keys require preserve_ids=True.
        copy_references:
            If True, recursively clone nested ``Model`` instances.
            If False, keep nested ``Model`` references as-is.

            ``Reference()`` fields usually represent stored identity.

    Returns:
        A reconstructed copy with valid Pydantic field and private state.

    Copying is local. Published FileStacks retain their immutable byte-version
    identity; the explicit publication API creates the new object's metadata
    and bytes, even when the source scientific record has been archived.
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
        # Nested records are copied only if copy_references=True.
        if not _is_root and not copy_references:
            return value

        from simstack.models.files import FileStack
        if isinstance(value, FileStack):
            with value._publication_lock():
                return _reconstruct_pydantic_model(
                    value, deep=deep, preserve_ids=preserve_ids,
                    copy_references=copy_references, _seen=_seen,
                )

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
    # Preserve field names, extras, private attributes and fields-set state
    # without re-running validators on local workflow caches.
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
        if field_name not in model_values:
            if field_info.is_required():
                raise ValueError(
                    f"Cannot copy {type(source).__name__}: missing required field {field_name!r}"
                )
            model_values[field_name] = field_info.get_default(call_default_factory=True)

    if isinstance(source, Model) and not preserve_ids:
        # Standard records use a generated ObjectId. Other primary-key
        # types must be retained explicitly rather than replaced by invalid data.
        if source.__primary_field__ != "id" or not isinstance(source.id, ObjectId):
            raise ValueError("Use preserve_ids=True for a custom primary key")
        model_values["id"] = ObjectId()
        # Fresh identities must be inserted in full, even when their source was
        # loaded from a repository and therefore carries an existing baseline.
        model_values.pop("_simstack_persisted_document", None)
        model_values.pop("_simstack_refreshed_fields", None)

    from simstack.util.object_list_mixin import ObjectListMixin
    if deep and copy_references and isinstance(source, ObjectListMixin):
        source_cache = source.__dict__.get("_cache", [])
        copied_cache = model_values.get("_cache", [])
        identities = {original.id: clone.id
                      for original, clone in zip(source_cache, copied_cache)}
        model_values["elements"] = [identities.get(identity, identity)
                                    for identity in source.elements]

    from simstack.models.dataset import DataSetSelection
    from simstack.models.datasettuple import DataSetTupleSection, DataSetTupleSelection
    from simstack.methods.multiple_executor import MultipleExecutorInput, MultipleExecutorOutput
    if deep and copy_references and isinstance(source, (DataSetSelection, DataSetTupleSelection)):
        source_cache = source.__dict__.get("_cache")
        copied_cache = model_values.get("_cache")
        if isinstance(source_cache, Model) and source.dataset_id == source_cache.id:
            model_values["dataset_id"] = copied_cache.id

    if deep and copy_references and isinstance(source, (DataSetTupleSection, MultipleExecutorInput)):
        source_cache = source.__dict__.get("_cache", [])
        copied_cache = model_values.get("_cache", [])
        identities = {(column, original.id): clone.id
                      for original_row, cloned_row in zip(source_cache, copied_cache)
                      for column, (original, clone) in enumerate(zip(original_row, cloned_row))}
        # Keep manually supplied/unmaterialized IDs and the copied containers'
        # aliases intact; only cloned records acquire a new reference identity.
        field_name = "data" if isinstance(source, DataSetTupleSection) else "inputs_data"
        for row in model_values[field_name]:
            row[:] = [identities.get((column, identity), identity) for column, identity in enumerate(row)]

    if deep and copy_references and isinstance(source, MultipleExecutorOutput):
        source_cache = source.__dict__.get("_cache", [])
        copied_cache = model_values.get("_cache", [])
        identities = {original.id: clone.id
                      for original, clone in zip(source_cache, copied_cache)
                      if isinstance(original, Model)}
        model_values["result_ids"][:] = [identities.get(identity, identity)
                                        for identity in source.result_ids]

    from simstack.models.files import FileStack
    if isinstance(source, FileStack):
        from simstack.models.parameters import Resource
        staged = getattr(source, "_staging_directory", None)
        if staged is not None:
            import atexit
            from pathlib import Path
            import shutil
            import tempfile

            directory = tempfile.mkdtemp(prefix="simstack-file-", dir=Path(staged).parent)
            atexit.register(shutil.rmtree, directory, ignore_errors=True)
            locations = [location.model_copy(deep=True) for location in source.locations]
            for location in locations:
                path = Path(location.path)
                if location.location_type == "local_path" and path.resolve().is_relative_to(Path(staged).resolve()):
                    target = Path(directory) / path.name
                    shutil.copyfile(path, target)
                    location.path = str(target.resolve())
                    location.resource = Resource(value="__pending__")
            copied.locations = locations
            object.__setattr__(copied, "_staging_directory", directory)
        elif copied.id != source.id and any(location.location_type == "s3" and location.status == "available" for location in source.locations):
            object.__setattr__(copied, "_copy_source_file_id", getattr(source, "_copy_source_file_id", source.id))
            # The location's immutable object key survives canonical archive.
            # Securing a new object belongs to publish_value/ensure_uploaded,
            # with the explicit publishing caller's authorization.

    return copied
