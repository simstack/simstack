"""Canonical task, claim, lease, and child DTOs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator

from ..constants import RUNNER_TASK_SCHEMA_VERSION, RunnerTaskStatus
from .base import (
    CanonicalString,
    CanonicalTimestamp,
    StrictRunnerDto,
    require_canonical_document_identity,
)
from .bootstrap import RunnerDefaultParametersV1


class RunnerDocumentDependencyV1(StrictRunnerDto):
    variable_mapping: CanonicalString
    reference: CanonicalString
    value: dict[str, Any]

    @model_validator(mode="after")
    def canonical_document_identity(self) -> "RunnerDocumentDependencyV1":
        require_canonical_document_identity(self.value, expected_id=self.reference)
        return self


class RunnerDocumentV1(RunnerDocumentDependencyV1):
    variable_name: CanonicalString
    dependencies: list[RunnerDocumentDependencyV1]


class RunnerTypedDocumentV1(RunnerDocumentDependencyV1):
    dependencies: list[RunnerDocumentDependencyV1]


class RunnerInfoFilesV1(StrictRunnerDto):
    elements: list[RunnerTypedDocumentV1]


class RunnerArtifactMappingV1(StrictRunnerDto):
    name: str
    regex_pattern: str
    function_mapping: str
    function_code: str


class RunnerArtifactPlanV1(StrictRunnerDto):
    required: bool
    mappings: list[RunnerArtifactMappingV1]
    child_artifacts: list[dict[str, Any]]


class RunnerTaskV1(StrictRunnerDto):
    task_schema_version: Literal[RUNNER_TASK_SCHEMA_VERSION]
    id: CanonicalString
    name: str
    status: RunnerTaskStatus
    custom_name: str | None
    version: str | None
    workflow_package: CanonicalString | None = None
    workflow_installation_id: CanonicalString | None = None
    workflow_revision: CanonicalString | None = None
    runtime_profile: CanonicalString | None = None
    project: str | None
    category: str | None
    description: str | None
    call_path: str | None
    assignment_rule_id: str | None
    assignment_rule_name: str | None
    assignment_pattern: str | None
    error: str | None
    message: str | None
    return_kind: str | None
    parent_ids: list[str]
    created_at: CanonicalTimestamp
    started_at: CanonicalTimestamp | None
    completed_at: CanonicalTimestamp | None
    job_id: str | None
    function_hash: str
    arg_hash: str
    func_mapping: str
    is_async: bool
    parameters: RunnerDefaultParametersV1
    artifact_plan: RunnerArtifactPlanV1
    info_files: RunnerInfoFilesV1
    inputs: list[RunnerDocumentV1]
    results: list[RunnerDocumentV1]


class RunnerClaimEnvelopeV1(StrictRunnerDto):
    claim_id: CanonicalString
    attempt: int
    lease_until: CanonicalTimestamp
    execution_token: CanonicalString
    execution_token_expires_at: CanonicalTimestamp
    task: RunnerTaskV1

    @model_validator(mode="after")
    def positive_attempt(self) -> "RunnerClaimEnvelopeV1":
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        return self


class RunnerActiveTasksV1(StrictRunnerDto):
    tasks: list[RunnerClaimEnvelopeV1]


class RunnerLeaseResponseV1(StrictRunnerDto):
    task_id: CanonicalString
    claim_id: CanonicalString
    attempt: int
    lease_until: CanonicalTimestamp
    execution_token: CanonicalString
    execution_token_expires_at: CanonicalTimestamp

    @model_validator(mode="after")
    def positive_attempt(self) -> "RunnerLeaseResponseV1":
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        return self


class RunnerChildResolveV1(StrictRunnerDto):
    request_id: CanonicalString
    cache_hit: bool
    child_id: CanonicalString
    status: RunnerTaskStatus
    task: RunnerTaskV1
    inline_claim: RunnerClaimEnvelopeV1 | None = None


class RunnerChildStatusV1(StrictRunnerDto):
    child_id: CanonicalString
    status: RunnerTaskStatus
    terminal: bool
    task: RunnerTaskV1


class RunnerTaskCacheV1(StrictRunnerDto):
    task_id: CanonicalString
    claim_id: CanonicalString
    attempt: int
    cache_hit: bool
    task: RunnerTaskV1 | None

    @model_validator(mode="after")
    def consistent_cache_snapshot(self) -> "RunnerTaskCacheV1":
        if self.attempt < 1 or self.cache_hit != (self.task is not None):
            raise ValueError("Cache response must contain exactly one snapshot on a hit")
        if self.task is not None and self.task.status != RunnerTaskStatus.COMPLETED:
            raise ValueError("Cached task snapshot must be completed")
        return self
