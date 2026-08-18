from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Optional

from odmantic import EmbeddedModel, Field, Model, ObjectId
from odmantic.bson import WithBsonSerializer
from pydantic import ConfigDict, field_validator


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_git_commit(value: str) -> str:
    normalized = value.strip().lower()
    if not _GIT_COMMIT_PATTERN.fullmatch(normalized):
        raise ValueError("commit must be a full 40- or 64-character Git object id")
    return normalized


class CodeSource(EmbeddedModel):
    """The exact repository commit containing registered workflow code."""

    repo_id: ObjectId
    commit: str

    @field_validator("commit")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        return normalize_git_commit(value)


def _bson_identity(value: Any) -> Any:
    # ODMantic 1.x otherwise misclassifies Optional[EmbeddedModel] as a list.
    return value


OptionalCodeSource = Annotated[
    Optional[CodeSource],
    WithBsonSerializer(_bson_identity),
]


class WorkflowRepoState(str, Enum):
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"


class WorkflowRepo(Model):
    """A complete user-owned Git repository stored in the user's database."""

    name: str = Field(unique=True)
    archive_bytes: bytes
    archive_sha256: str = Field(unique=True)
    head_commit: str
    state: WorkflowRepoState = WorkflowRepoState.VALIDATING
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("repository name must not be empty")
        return normalized

    @field_validator("archive_sha256")
    @classmethod
    def validate_archive_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("archive_sha256 must be a lowercase SHA-256 digest")
        return normalized

    @field_validator("head_commit")
    @classmethod
    def validate_head_commit(cls, value: str) -> str:
        return normalize_git_commit(value)

    model_config = ConfigDict(collection="workflow_repos")
