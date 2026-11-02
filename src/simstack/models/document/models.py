"""Pydantic records and embedded values, without an engine or active record API."""

from __future__ import annotations

from collections.abc import Mapping
from types import UnionType
from typing import Annotated, Any, ClassVar, Union, get_args, get_origin
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field as PydanticField, model_validator

from simstack.persistence.criteria import FieldPath

from .codec import DocumentParsingError, decode_document_value, encode_document_value
from .fields import PRIMARY_SCHEMA_KEY, REFERENCE_SCHEMA_KEY
from .identity import ObjectId
from .schema import annotate_document_schema, default_collection_name

SUBTYPE_KEY = "__simstack_model__"
_MODEL_TYPES: WeakValueDictionary[str, type] = WeakValueDictionary()


def _type_name(model: type) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def resolve_document_type(declared: type, value: Any) -> type:
    """Resolve only a loaded, registered subtype; never import a wire type name."""
    marker = value.get(SUBTYPE_KEY) if isinstance(value, Mapping) else None
    if marker is None:
        return declared
    actual = _MODEL_TYPES.get(marker) if isinstance(marker, str) else None
    if actual is None or not issubclass(actual, declared):
        raise DocumentParsingError(
            f"Unknown or incompatible document subtype: {marker!r}"
        )
    return actual


def _declared_model(annotation: Any) -> type | None:
    annotation = _unwrap_annotation(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, UnionType):
        targets = [
            _declared_model(arg)
            for arg in get_args(annotation)
            if arg is not type(None)
        ]
        targets = [target for target in targets if target is not None]
        return targets[0] if len(targets) == 1 else None
    return None


def _unwrap_annotation(annotation):
    if get_origin(annotation) is Annotated:
        return _unwrap_annotation(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        candidates = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(candidates) == 1:
            return _unwrap_annotation(candidates[0])
    return annotation


class _DomainMeta(type(BaseModel)):
    def __getattribute__(cls, name):
        if name == "id":
            try:
                primary = super().__getattribute__("__primary_field__")
            except AttributeError:
                primary = "id"
            if primary != "id":
                return FieldPath("id")
        return super().__getattribute__(name)

    def __new__(mcls, name, bases, namespace, **kwargs):
        # Custom primary keys are ordinary validated fields. The neutral `id`
        # accessor points to that field, retaining its public constructor name.
        primary = [
            key
            for key, field in namespace.items()
            if isinstance(getattr(field, "json_schema_extra", None), dict)
            and field.json_schema_extra.get(PRIMARY_SCHEMA_KEY)
        ]
        if len(primary) > 1:
            raise TypeError("A document can declare only one primary field")
        if primary and primary[0] != "id":
            key = primary[0]
            namespace["__annotations__"] = {
                **namespace.get("__annotations__", {}),
                "id": ClassVar[Any],
            }
            namespace["id"] = property(
                lambda self: getattr(self, type(self).__primary_field__)
            )
            namespace["__primary_field__"] = key
        return super().__new__(mcls, name, bases, namespace, **kwargs)

    def __getattr__(cls, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            fields = cls.__dict__.get("__pydantic_fields__", {})
            if name in fields:
                field = fields[name]
                primary = getattr(cls, "__primary_field__", "id")
                return FieldPath(
                    "id"
                    if name == primary
                    else field.serialization_alias or field.alias or name
                )
            raise


def _selection(selection, field):
    selected = selection.get(field) if isinstance(selection, dict) else None
    return None if selected is True or selected is Ellipsis else selected


_MISSING_SELECTION = object()


def _container_selection(selection, key, length=None):
    if selection is None:
        return _MISSING_SELECTION
    if isinstance(selection, set):
        return (
            True
            if key in selection or (length is not None and key - length in selection)
            else _MISSING_SELECTION
        )
    if isinstance(selection, dict):
        common = selection.get("__all__", _MISSING_SELECTION)
        specific = selection.get(
            key,
            selection.get(key - length, _MISSING_SELECTION)
            if length is not None
            else _MISSING_SELECTION,
        )
        if isinstance(common, dict) and isinstance(specific, dict):
            return {**common, **specific}
        return common if specific is _MISSING_SELECTION else specific
    return selection


def _selected_children(values, include, exclude):
    length = len(values) if isinstance(values, (list, tuple)) else None
    entries = enumerate(values) if length is not None else values.items()
    for key, item in entries:
        included = _container_selection(include, key, length)
        excluded = _container_selection(exclude, key, length)
        if include is not None and included is _MISSING_SELECTION:
            continue
        if excluded is True or excluded is Ellipsis:
            continue
        yield (
            key,
            item,
            None
            if included is True
            or included is Ellipsis
            or included is _MISSING_SELECTION
            else included,
            None if excluded is _MISSING_SELECTION else excluded,
        )


def _dump_value(value, annotation=None, *, reference=False, include=None, exclude=None):
    annotation = _unwrap_annotation(annotation)
    declared = _declared_model(annotation)
    subtype = (
        declared is not None
        and isinstance(value, BaseModel)
        and type(value) is not declared
    )
    if reference and isinstance(value, Model):
        identity = value.id
        return (
            {"id": identity, SUBTYPE_KEY: _type_name(type(value))}
            if subtype
            else identity
        )
    if isinstance(value, BaseModel):
        dumped = BaseModel.model_dump(
            value,
            mode="python",
            by_alias=True,
            serialize_as_any=True,
            include=include,
            exclude=exclude,
        )
        if not isinstance(dumped, dict):
            return dumped
        values = object.__getattribute__(value, "__dict__")
        serializers = getattr(type(value), "__pydantic_decorators__", None)
        serialized_fields = {
            name
            for decorator in getattr(serializers, "field_serializers", {}).values()
            for name in decorator.info.fields
        }
        for name, field in type(value).model_fields.items():
            key = field.serialization_alias or field.alias or name
            if key not in dumped:
                continue
            if name in values and name not in serialized_fields:
                dumped[key] = _dump_value(
                    values[name],
                    field.annotation,
                    reference=bool(
                        (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY)
                    ),
                    include=_selection(include, name),
                    exclude=_selection(exclude, name),
                )
        if isinstance(value, Model):
            primary = type(value).__primary_field__
            primary_field = type(value).model_fields[primary]
            key = primary_field.serialization_alias or primary_field.alias or primary
            if key in dumped:
                dumped["id"] = dumped.pop(key)
        shared_collection_subtype = isinstance(value, Model) and any(
            isinstance(base, type)
            and issubclass(base, Model)
            and getattr(base, "__collection__", None) == type(value).__collection__
            for base in type(value).__mro__[1:]
            if base is not Model
        )
        if subtype or shared_collection_subtype:
            dumped[SUBTYPE_KEY] = _type_name(type(value))
        return dumped
    origin, args = get_origin(annotation), get_args(annotation)
    if isinstance(value, Mapping):
        child_type = args[1] if origin is dict and len(args) == 2 else None
        return {
            key: _dump_value(
                item,
                child_type,
                reference=reference,
                include=child_include,
                exclude=child_exclude,
            )
            for key, item, child_include, child_exclude in _selected_children(
                value, include, exclude
            )
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        result = []
        sequence = value if isinstance(value, (list, tuple)) else list(value)
        for index, item, child_include, child_exclude in _selected_children(
            sequence, include, exclude
        ):
            child_type = (
                (
                    args[index]
                    if origin is tuple and len(args) > 1 and args[-1] is not Ellipsis
                    else args[0]
                )
                if args
                else None
            )
            result.append(
                _dump_value(
                    item,
                    child_type,
                    reference=reference,
                    include=child_include,
                    exclude=child_exclude,
                )
            )
        return result
    return value


class _DomainValue(BaseModel, metaclass=_DomainMeta):
    """A validated embedded value. Custom workflow hooks remain ordinary methods."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,
        validate_assignment=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        super().__pydantic_init_subclass__(**kwargs)
        _MODEL_TYPES[_type_name(cls)] = cls

    @model_validator(mode="wrap")
    @classmethod
    def _validate_domain_type(cls, value, handler):
        if isinstance(value, Mapping) and SUBTYPE_KEY in value:
            actual = resolve_document_type(cls, value)
            value = {key: item for key, item in value.items() if key != SUBTYPE_KEY}
            if actual is not cls:
                return actual.model_validate(value)
        return handler(value)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return annotate_document_schema(
            cls, super().__get_pydantic_json_schema__(core_schema, handler)
        )

    @classmethod
    def model_validate_doc(cls, value: Any):
        try:
            return cls.model_validate(decode_document_value(value))
        except DocumentParsingError:
            raise
        except (TypeError, ValueError) as exc:
            raise DocumentParsingError(
                f"Could not parse {cls.__name__} document"
            ) from exc

    def model_dump_doc(self, *, include=None, exclude=None, **kwargs) -> dict[str, Any]:
        if kwargs:
            raise TypeError("Unsupported document dump options: " + ", ".join(kwargs))
        return encode_document_value(
            _dump_value(self, include=include, exclude=exclude)
        )


class EmbeddedModel(_DomainValue):
    """A value embedded in another record; it has no independent identity."""


class Model(_DomainValue):
    """Pydantic domain record with neutral string identity and schema metadata."""

    __collection__: ClassVar[str]
    __primary_field__: ClassVar[str] = "id"
    id: ObjectId = PydanticField(
        default_factory=ObjectId, alias="_id", serialization_alias="id"
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        super().__pydantic_init_subclass__(**kwargs)
        cls.__collection__ = str(
            cls.model_config.get("collection") or default_collection_name(cls.__name__)
        )

    @model_validator(mode="before")
    @classmethod
    def _identity_alias(cls, value):
        if isinstance(value, Mapping):
            value = dict(value)
            identity = value.pop("_id", value.get("id", None))
            if cls.__primary_field__ != "id":
                value.pop("id", None)
                if identity is not None:
                    value.setdefault(cls.__primary_field__, identity)
            elif identity is not None:
                value["id"] = identity
        return value


__all__ = ["EmbeddedModel", "Model", "SUBTYPE_KEY", "resolve_document_type"]
