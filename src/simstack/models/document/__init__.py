"""Persistence-neutral Pydantic domain infrastructure.

The package contains no database client, engine or active-record behavior.
Concrete persistence adapters live in ``simstack-server``; runner processes use
the execution HTTP API.  The exports here keep model declarations concise while
the implementation remains split by responsibility.
"""

from .codec import (
    DocumentParsingError,
    decode_document_value,
    encode_document_value,
)
from .fields import (
    PAYLOAD_SCHEMA_KEY,
    REFERENCE_MODEL_SCHEMA_KEY,
    REFERENCE_SCHEMA_KEY,
    Field,
    Reference,
)
from .identity import ObjectId
from .models import EmbeddedModel, Model, SUBTYPE_KEY, resolve_document_type


__all__ = [
    "DocumentParsingError",
    "EmbeddedModel",
    "Field",
    "Model",
    "ObjectId",
    "PAYLOAD_SCHEMA_KEY",
    "REFERENCE_MODEL_SCHEMA_KEY",
    "REFERENCE_SCHEMA_KEY",
    "Reference",
    "SUBTYPE_KEY",
    "resolve_document_type",
    "decode_document_value",
    "encode_document_value",
]
