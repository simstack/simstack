"""Validated response values returned by the scoped runner API clients."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ClaimedTask:
    task: dict[str, Any]
    claim_id: str
    attempt: int
    lease_until: str
    execution_token: str = field(repr=False)
    execution_token_expires_at: str


@dataclass(frozen=True)
class ResolvedChildTask:
    request_id: str
    cache_hit: bool
    child_id: str
    status: str
    task: dict[str, Any]
    inline_claim: ClaimedTask | None = None


@dataclass(frozen=True)
class ChildTaskState:
    child_id: str
    status: str
    terminal: bool
    task: dict[str, Any]
