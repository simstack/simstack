from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from odmantic import Field, Index, Model, ObjectId
from pydantic import ConfigDict


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GeneratedWorkflowStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    INSTALLING = "installing"
    READY = "ready"
    FAILED = "failed"


class GeneratedWorkflowSource(Model):
    """One immutable source revision produced by the AI workflow authoring flow.

    ``source_code`` and ``source_sha256`` form the immutable payload. Lifecycle
    fields may change while the exact revision is installed on a runner.
    """

    workflow_id: str
    revision: int = Field(ge=1)
    title: str
    description: str = ""
    namespace: str
    module_name: str
    entrypoint_name: str
    source_code: str
    source_sha256: str
    target_resource: str
    authoring_mode: Literal["user", "expert"] = "user"
    status: GeneratedWorkflowStatus = GeneratedWorkflowStatus.DRAFT
    codex_thread_id: Optional[str] = None
    install_task_id: Optional[ObjectId] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = ConfigDict(
        collection="generated_workflow_source",
        indexes=lambda: [
            Index(
                GeneratedWorkflowSource.workflow_id,
                GeneratedWorkflowSource.revision,
                unique=True,
            )
        ],
    )
