"""Pydantic base classes for SimStack domain documents and embedded values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from .codec import DocumentParsingError, decode_document_value, encode_document_value
from .fields import REFERENCE_SCHEMA_KEY
from .identity import ObjectId
from .schema import annotate_document_schema, default_collection_name


def _dump_reference_value(value: Any) -> Any:
    """Flatten reference leaves without changing their surrounding containers."""
    if isinstance(value, Model):
        return str(value.id)
    if isinstance(value, Mapping):
        return {str(key): _dump_reference_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_dump_reference_value(item) for item in value]
    return encode_document_value(value)


def _dump_document_value(value: Any) -> Any:
    """Flatten declared references throughout embedded document containers."""
    if isinstance(value, BaseModel):
        document = {}
        fields = object.__getattribute__(value, "__dict__")
        for field_name, field in type(value).model_fields.items():
            item = fields[field_name]
            if (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                document[field.alias or field_name] = _dump_reference_value(item)
            else:
                document[field.alias or field_name] = _dump_document_value(item)
        document.update({key: _dump_document_value(item) for key, item in (getattr(value, "__pydantic_extra__", None) or {}).items()})
        return document
    if isinstance(value, Mapping):
        return {str(key): _dump_document_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_dump_document_value(item) for item in value]
    return encode_document_value(value)


class EmbeddedModel(BaseModel):
    """Persistence-neutral embedded value object."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,
        validate_assignment=True,
    )

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any):
        schema = super().__get_pydantic_json_schema__(core_schema, handler)
        return annotate_document_schema(cls, schema)


class Model(BaseModel):
    """Persistence-neutral Pydantic document.

    This is domain infrastructure, not an ODM: it owns validation, identity and
    serialization only.  It intentionally has no database engine and no active
    record methods such as ``save`` or ``find``.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,
        validate_assignment=True,
    )

    id: ObjectId = PydanticField(default_factory=ObjectId, alias="_id", serialization_alias="id")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        configured_collection = cls.model_config.get("collection")
        cls.__collection__ = str(
            configured_collection or default_collection_name(cls.__name__)
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any):
        schema = super().__get_pydantic_json_schema__(core_schema, handler)
        return annotate_document_schema(cls, schema)

    @classmethod
    def model_validate_doc(cls, value: Any) -> "Model":
        try:
            decoded = decode_document_value(value)
            return cls.model_validate(decoded)
        except DocumentParsingError:
            raise
        except (TypeError, ValueError) as exc:
            raise DocumentParsingError(
                f"Could not parse {cls.__name__} document"
            ) from exc

    def model_dump_doc(self) -> dict[str, Any]:
        """Return a JSON-safe document for a persistence or transport adapter."""

        return _dump_document_value(self)


__all__ = ["EmbeddedModel", "Model"]
