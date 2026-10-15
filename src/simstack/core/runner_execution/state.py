"""Claim-scoped state and immutable execution result values."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from .errors import RunnerTaskExecutionError


@dataclass(frozen=True)
class RunnerClaimContext:
    task_id: str
    claim_id: str
    attempt: int
    call_path: str | None = None


@dataclass(frozen=True)
class RunnerCommitPayload:
    results: list[dict[str, Any]]
    info_files: dict[str, Any] | None
    message: str | None
    return_kind: str
    custom_name: str | None


_current_claim: ContextVar[RunnerClaimContext | None] = ContextVar(
    "simstack_runner_claim", default=None
)


def current_runner_claim() -> RunnerClaimContext:
    claim = _current_claim.get()
    if claim is None:
        raise RunnerTaskExecutionError(
            "A decorated node was called in runner mode without an active parent claim"
        )
    return claim


def activate_runner_claim(claim: RunnerClaimContext) -> Token:
    return _current_claim.set(claim)


def reset_runner_claim(token: Token) -> None:
    _current_claim.reset(token)
