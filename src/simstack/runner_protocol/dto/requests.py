"""Exact request and mutation-response DTOs for runner API v1."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, model_validator

from ..constants import (
    DEFAULT_RUNNER_LEASE_SECONDS,
    MAX_RUNNER_LEASE_SECONDS,
    MIN_RUNNER_LEASE_SECONDS,
    RunnerEventType,
    RunnerProcessType,
    RunnerTaskStatus,
)
from .base import (
    CanonicalString,
    CanonicalTimestamp,
    StrictRunnerDto,
    require_canonical_document_identity,
)


class RunnerTaskClaimRequestV1(StrictRunnerDto):
    request_id: CanonicalString = Field(max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    lease_seconds: int = Field(
        default=DEFAULT_RUNNER_LEASE_SECONDS,
        ge=MIN_RUNNER_LEASE_SECONDS,
        le=MAX_RUNNER_LEASE_SECONDS,
    )
    runner_instance_id: CanonicalString | None = Field(default=None, max_length=200)


class RunnerTaskLeaseRequestV1(StrictRunnerDto):
    claim_id: CanonicalString = Field(max_length=200)
    attempt: int = Field(ge=1)
    lease_seconds: int = Field(
        default=DEFAULT_RUNNER_LEASE_SECONDS,
        ge=MIN_RUNNER_LEASE_SECONDS,
        le=MAX_RUNNER_LEASE_SECONDS,
    )


class RunnerTaskCacheRequestV1(StrictRunnerDto):
    claim_id: CanonicalString = Field(max_length=200)
    attempt: int = Field(ge=1)


class RunnerSlurmInfoV1(StrictRunnerDto):
    job_id: CanonicalString = Field(max_length=500)
    name: str = Field(default="", max_length=500)
    user: str = Field(default="", max_length=500)
    code: str = Field(default="", max_length=100)
    time: str = Field(default="", max_length=100)
    nodes: list[str] = Field(default_factory=list, max_length=10_000)
    updated: CanonicalTimestamp


class RunnerTaskStatusRequestV1(StrictRunnerDto):
    claim_id: CanonicalString = Field(max_length=200)
    attempt: int = Field(ge=1)
    expected_status: RunnerTaskStatus
    status: RunnerTaskStatus
    observed_at: CanonicalTimestamp
    message: str | None = Field(default=None, max_length=10_000)
    job_id: str | None = Field(default=None, max_length=500)
    slurm_info: RunnerSlurmInfoV1 | None = None
    function_hash: CanonicalString | None = Field(default=None, max_length=1_000)
    arg_hash: CanonicalString | None = Field(default=None, max_length=1_000)
    version: str | None = Field(default=None, max_length=200)
    is_async: bool | None = None

    @model_validator(mode="after")
    def observation_time_is_canonical(self) -> "RunnerTaskStatusRequestV1":
        identity_fields = (self.function_hash, self.arg_hash, self.is_async)
        if (self.version is not None or any(value is not None for value in identity_fields)) and not all(
            value is not None for value in identity_fields
        ):
            raise ValueError("Execution identity requires function_hash, arg_hash and is_async")
        observed_at = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        if self.slurm_info is not None and datetime.fromisoformat(
            self.slurm_info.updated.replace("Z", "+00:00")
        ).astimezone(timezone.utc) != observed_at.astimezone(timezone.utc):
            raise ValueError("slurm_info.updated must equal observed_at")
        return self


class RunnerDependencyDocumentInputV1(StrictRunnerDto):
    variable_mapping: CanonicalString = Field(max_length=1_000)
    reference: CanonicalString
    value: dict[str, Any]

    @model_validator(mode="after")
    def canonical_identity(self) -> "RunnerDependencyDocumentInputV1":
        require_canonical_document_identity(self.value, expected_id=self.reference)
        return self


class RunnerResultDocumentInputV1(StrictRunnerDto):
    variable_name: CanonicalString = Field(max_length=500)
    variable_mapping: CanonicalString = Field(max_length=1_000)
    value: dict[str, Any]
    dependencies: list[RunnerDependencyDocumentInputV1]

    @model_validator(mode="after")
    def canonical_identity(self) -> "RunnerResultDocumentInputV1":
        require_canonical_document_identity(self.value)
        return self


class RunnerTypedDocumentInputV1(StrictRunnerDto):
    variable_mapping: CanonicalString = Field(max_length=1_000)
    value: dict[str, Any]
    dependencies: list[RunnerDependencyDocumentInputV1]

    @model_validator(mode="after")
    def canonical_identity(self) -> "RunnerTypedDocumentInputV1":
        require_canonical_document_identity(self.value)
        return self


class RunnerInfoFilesInputV1(StrictRunnerDto):
    elements: list[RunnerTypedDocumentInputV1]


class RunnerTaskCommitRequestV1(StrictRunnerDto):
    claim_id: CanonicalString = Field(max_length=200)
    attempt: int = Field(ge=1)
    expected_status: Literal[RunnerTaskStatus.RUNNING]
    results: list[RunnerResultDocumentInputV1]
    info_files: RunnerInfoFilesInputV1 | None
    message: str | None = Field(max_length=10_000)
    return_kind: Literal["bool", "model", "multiple"] | None = None
    custom_name: str | None = Field(default=None, max_length=10_000)
    expected_custom_name: str | None = Field(default=None, max_length=10_000)


class RunnerTaskFailRequestV1(StrictRunnerDto):
    claim_id: CanonicalString = Field(max_length=200)
    attempt: int = Field(ge=1)
    error: CanonicalString = Field(max_length=100_000)
    message: str | None = Field(default=None, max_length=10_000)


class RunnerHeartbeatRequestV1(StrictRunnerDto):
    hostname: str | None = Field(default=None, max_length=500)
    pid: int | None = Field(default=None, ge=1)
    message: str | None = Field(default=None, max_length=10_000)
    git_status: list[str] = Field(default_factory=list, max_length=1_000)
    runner_version: str | None = Field(default=None, max_length=200)


class RunnerLogInputV1(StrictRunnerDto):
    timestamp: CanonicalTimestamp
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    logger_name: CanonicalString = Field(max_length=500)
    message: str = Field(max_length=100_000)
    module: str = Field(max_length=500)
    function: str = Field(max_length=500)
    line: int = Field(ge=0)
    task_id: str | None = Field(default=None, max_length=200)
    thread_name: str | None = Field(default=None, max_length=500)
    process_name: str | None = Field(default=None, max_length=500)
    exception_type: str | None = Field(default=None, max_length=500)
    exception_message: str | None = Field(default=None, max_length=100_000)
    exception_traceback: str | None = Field(default=None, max_length=500_000)


class RunnerEventInputV1(StrictRunnerDto):
    timestamp: CanonicalTimestamp
    event: RunnerEventType
    runner_type: RunnerProcessType
    node_id: str | None = None
    pid: int | None = Field(default=None, ge=1)
    hostname: str | None = Field(default=None, max_length=500)
    user: str | None = Field(default=None, max_length=500)
    message: str | None = Field(default=None, max_length=10_000)
    git_status: list[str] = Field(default_factory=list, max_length=1_000)


class RunnerLogBatchRequestV1(StrictRunnerDto):
    logs: list[RunnerLogInputV1] = Field(max_length=1_000)
    events: list[RunnerEventInputV1] = Field(max_length=1_000)

    @model_validator(mode="after")
    def require_entries(self) -> "RunnerLogBatchRequestV1":
        if not self.logs and not self.events:
            raise ValueError("At least one log or event is required")
        return self


class RunnerChildNodeSpecV1(StrictRunnerDto):
    name: CanonicalString = Field(max_length=500)
    function_mapping: CanonicalString = Field(max_length=1_000)
    function_hash: CanonicalString = Field(max_length=1_000)
    arg_hash: CanonicalString = Field(max_length=1_000)
    call_path: CanonicalString = Field(max_length=4_000)
    parameters: dict[str, Any]
    custom_name: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=10_000)
    version: str | None = Field(default=None, max_length=200)
    is_async: bool


class RunnerChildResolveRequestV1(StrictRunnerDto):
    claim_id: CanonicalString = Field(max_length=200)
    attempt: int = Field(ge=1)
    request_id: CanonicalString = Field(max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    node: RunnerChildNodeSpecV1
    inputs: list[RunnerResultDocumentInputV1]
    prefer_inline: bool = False


class RunnerTaskMutationResponseV1(StrictRunnerDto):
    task_id: CanonicalString
    status: RunnerTaskStatus
    accepted: bool


class RunnerTaskCommitResponseV1(RunnerTaskMutationResponseV1):
    result_references: list[dict[str, Any]]


class RunnerHeartbeatResponseV1(StrictRunnerDto):
    accepted: bool
    server_time: datetime
    resource: CanonicalString


class RunnerLogBatchResponseV1(StrictRunnerDto):
    logs_accepted: int = Field(ge=0)
    events_accepted: int = Field(ge=0)
