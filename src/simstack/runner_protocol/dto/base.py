"""Strict primitives shared by all runner wire DTOs."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Strict


def _canonical_string(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("value must be a non-empty canonical string")
    return value


CanonicalString = Annotated[str, Strict(), AfterValidator(_canonical_string)]


def _canonical_timestamp(value: str) -> str:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("value must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


CanonicalTimestamp = Annotated[
    str,
    Strict(),
    AfterValidator(_canonical_string),
    AfterValidator(_canonical_timestamp),
]


def require_canonical_document_identity(
    value: dict[str, Any],
    *,
    expected_id: str | None = None,
) -> None:
    """Reject storage aliases and ID-less documents at the runner wire boundary."""

    document_id = value.get("id")
    if "_id" in value or not isinstance(document_id, str):
        raise ValueError("document value must contain exactly one canonical id")
    _canonical_string(document_id)
    if expected_id is not None and document_id != expected_id:
        raise ValueError("document value must use the matching canonical id")


class StrictRunnerDto(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
