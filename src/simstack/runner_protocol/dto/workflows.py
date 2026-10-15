"""Leased, resource-bound workflow installation messages."""

from typing import Literal

from pydantic import Field

from .base import CanonicalString, CanonicalTimestamp, StrictRunnerDto
from .bootstrap import RunnerWorkflowPackageV1


class RunnerWorkflowClaimRequestV1(StrictRunnerDto):
    request_id: CanonicalString
    lease_seconds: int = Field(default=600, gt=0)


class RunnerWorkflowClaimV1(RunnerWorkflowPackageV1):
    claim_id: CanonicalString
    attempt: int = Field(ge=1)
    lease_expires_at: CanonicalTimestamp


class RunnerWorkflowStatusRequestV1(StrictRunnerDto):
    workflow_revision: CanonicalString = Field(pattern=r"^[0-9a-f]{64}$")
    claim_id: CanonicalString
    attempt: int = Field(ge=1)
    status: Literal["ready", "failed"]
    error: str | None = None
