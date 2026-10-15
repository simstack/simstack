"""Convert strict protocol DTOs into the public runner API result values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from simstack.runner_protocol import (
    RunnerChildResolveV1,
    RunnerChildStatusV1,
    validate_runner_claim_envelope_v1,
)

from .base import BaseRunnerClient
from .models import ChildTaskState, ClaimedTask, ResolvedChildTask


def claimed_task(
    client: BaseRunnerClient,
    payload: Mapping[str, Any],
    *,
    operation: str,
) -> ClaimedTask:
    parsed = client._protocol(
        validate_runner_claim_envelope_v1,
        dict(payload),
        operation=operation,
    )
    return ClaimedTask(
        task=parsed["task"],
        claim_id=parsed["claim_id"],
        attempt=parsed["attempt"],
        lease_until=parsed["lease_until"],
        execution_token=parsed["execution_token"],
        execution_token_expires_at=parsed["execution_token_expires_at"],
    )


def resolved_child(
    client: BaseRunnerClient,
    payload: Mapping[str, Any],
) -> ResolvedChildTask:
    parsed = client._wire_dto(
        RunnerChildResolveV1,
        payload,
        operation="child task resolution response",
    )
    return ResolvedChildTask(
        request_id=parsed.request_id,
        cache_hit=parsed.cache_hit,
        child_id=parsed.child_id,
        status=parsed.status.value,
        task=parsed.task.model_dump(mode="json"),
        inline_claim=(claimed_task(client, parsed.inline_claim.model_dump(mode="json"),
                                   operation="inline child claim") if parsed.inline_claim is not None else None),
    )


def child_state(
    client: BaseRunnerClient,
    payload: Mapping[str, Any],
) -> ChildTaskState:
    parsed = client._wire_dto(
        RunnerChildStatusV1,
        payload,
        operation="child task status response",
    )
    return ChildTaskState(
        child_id=parsed.child_id,
        status=parsed.status.value,
        terminal=parsed.terminal,
        task=parsed.task.model_dump(mode="json"),
    )
