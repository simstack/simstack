"""Claim-bound execution-token runner API client."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from simstack.runner_protocol import (
    DEFAULT_RUNNER_LEASE_SECONDS,
    RunnerChildResolveRequestV1,
    RunnerTaskCacheRequestV1,
    RunnerTaskCacheV1,
    RunnerTaskCommitRequestV1,
    RunnerTaskCommitResponseV1,
    RunnerTaskFailRequestV1,
    RunnerTaskLeaseRequestV1,
    RunnerTaskMutationResponseV1,
    RunnerTaskStatus,
    RunnerTaskStatusRequestV1,
    child_path,
    child_resolve_path,
    task_bootstrap_path,
    task_cache_path,
    task_commit_path,
    task_fail_path,
    task_lease_path,
    task_path,
    task_status_path,
    validate_attempt,
    validate_runner_bootstrap_v1,
    validate_runner_lease_response_v1,
)

from .base import BaseRunnerClient
from .errors import RunnerApiError
from .models import ChildTaskState, ClaimedTask, ResolvedChildTask
from .parsing import child_state, claimed_task, resolved_child


class ExecutionRunnerClient(BaseRunnerClient):
    """Only operations authorized for one task/claim/attempt execution token."""

    def _attempt(self, attempt: int) -> None:
        self._protocol(validate_attempt, attempt)

    async def task_bootstrap(
        self,
        task_id: str,
        *,
        claim_id: str,
        attempt: int,
    ) -> dict[str, Any]:
        self._attempt(attempt)
        path = self._protocol(
            task_bootstrap_path,
            task_id,
            claim_id=claim_id,
            attempt=attempt,
        )
        payload = self._require_object(
            await self._request("GET", path, retry_safe=True),
            operation="task bootstrap",
        )
        parsed = self._protocol(
            validate_runner_bootstrap_v1,
            payload,
            operation="task bootstrap response",
        )
        self._configure_capabilities(
            parsed["capabilities"], operation="task bootstrap response"
        )
        return parsed

    async def get_task(
        self,
        task_id: str,
        *,
        claim_id: str,
        attempt: int,
    ) -> ClaimedTask:
        self._attempt(attempt)
        path = self._protocol(
            task_path,
            task_id,
            claim_id=claim_id,
            attempt=attempt,
        )
        payload = self._require_object(
            await self._request("GET", path, retry_safe=True),
            operation="claimed task read",
        )
        return claimed_task(self, payload, operation="claimed task read")

    async def resolve_child(
        self,
        parent_id: str,
        *,
        claim_id: str,
        attempt: int,
        request_id: str,
        node: Mapping[str, Any],
        inputs: Sequence[Mapping[str, Any]],
        prefer_inline: bool = False,
    ) -> ResolvedChildTask:
        self._attempt(attempt)
        path = self._protocol(child_resolve_path, parent_id)
        body = self._wire_body(
            RunnerChildResolveRequestV1,
            {
                "claim_id": claim_id,
                "attempt": attempt,
                "request_id": request_id,
                "node": dict(node),
                "inputs": [dict(item) for item in inputs],
                "prefer_inline": prefer_inline,
            },
            operation="child task resolution request",
        )
        payload = self._require_object(
            await self._request(
                "POST",
                path,
                body,
                retry_safe=True,
            ),
            operation="child task resolution",
        )
        return resolved_child(self, payload)

    async def resolve_task_cache(self, task_id: str, *, claim_id: str, attempt: int) -> dict[str, Any] | None:
        """Resolve cached output through the active claim, never local storage."""
        self._attempt(attempt)
        body = self._wire_body(RunnerTaskCacheRequestV1, {
            "claim_id": claim_id, "attempt": attempt,
        }, operation="task cache resolution request")
        payload = self._require_object(await self._request(
            "POST", self._protocol(task_cache_path, task_id), body, retry_safe=True,
        ), operation="task cache resolution")
        response = self._wire_dto(RunnerTaskCacheV1, payload, operation="task cache resolution response")
        if (response.task_id, response.claim_id, response.attempt) != (task_id, claim_id, attempt):
            raise RunnerApiError("Task cache response does not match the active claim")
        return response.task.model_dump(mode="json") if response.task is not None else None

    async def get_child(
        self,
        parent_id: str,
        child_id: str,
        *,
        claim_id: str,
        attempt: int,
    ) -> ChildTaskState:
        self._attempt(attempt)
        path = self._protocol(
            child_path,
            parent_id,
            child_id,
            claim_id=claim_id,
            attempt=attempt,
        )
        payload = self._require_object(
            await self._request("GET", path, retry_safe=True),
            operation="child task read",
        )
        return child_state(self, payload)

    async def renew_lease(
        self,
        task_id: str,
        *,
        claim_id: str,
        attempt: int,
        lease_seconds: int = DEFAULT_RUNNER_LEASE_SECONDS,
    ) -> dict[str, Any]:
        self._attempt(attempt)
        path = self._protocol(task_lease_path, task_id)
        body = self._wire_body(
            RunnerTaskLeaseRequestV1,
            {
                "claim_id": claim_id,
                "attempt": attempt,
                "lease_seconds": lease_seconds,
            },
            operation="lease renewal request",
        )
        payload = self._require_object(
            await self._request(
                "POST",
                path,
                body,
                retry_safe=True,
            ),
            operation="lease renewal",
        )
        parsed = self._protocol(
            validate_runner_lease_response_v1,
            payload,
            task_id=task_id,
            claim_id=claim_id,
            attempt=attempt,
        )
        self._replace_token(parsed["execution_token"])
        return parsed

    async def update_task_status(
        self,
        task_id: str,
        *,
        claim_id: str,
        attempt: int,
        expected_status: str,
        status: str,
        observed_at: str,
        message: str | None = None,
        job_id: str | None = None,
        slurm_info: Mapping[str, Any] | None = None,
        function_hash: str | None = None,
        arg_hash: str | None = None,
        version: str | None = None,
        is_async: bool | None = None,
    ) -> dict[str, Any]:
        self._attempt(attempt)
        body = self._wire_body(
            RunnerTaskStatusRequestV1,
            {
                "claim_id": claim_id,
                "attempt": attempt,
                "expected_status": expected_status,
                "status": status,
                "observed_at": observed_at,
                "message": message,
                "job_id": job_id,
                "slurm_info": dict(slurm_info) if slurm_info is not None else None,
                "function_hash": function_hash,
                "arg_hash": arg_hash,
                "version": version,
                "is_async": is_async,
            },
            operation="task status update request",
        )
        path = self._protocol(task_status_path, task_id)
        response = self._require_object(
            await self._request("POST", path, body, retry_safe=True),
            operation="task status update",
        )
        return self._wire_dto(
            RunnerTaskMutationResponseV1,
            response,
            operation="task status update response",
        ).model_dump(mode="json")

    async def fail_task(
        self,
        task_id: str,
        *,
        claim_id: str,
        attempt: int,
        error: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        self._attempt(attempt)
        body = self._wire_body(
            RunnerTaskFailRequestV1,
            {
                "claim_id": claim_id,
                "attempt": attempt,
                "error": error,
                "message": message,
            },
            operation="task failure request",
        )
        path = self._protocol(task_fail_path, task_id)
        response = self._require_object(
            await self._request("POST", path, body),
            operation="task failure report",
        )
        return self._wire_dto(
            RunnerTaskMutationResponseV1,
            response,
            operation="task failure response",
        ).model_dump(mode="json")

    async def commit_task(
        self,
        task_id: str,
        *,
        claim_id: str,
        attempt: int,
        expected_status: str = RunnerTaskStatus.RUNNING,
        results: Sequence[Mapping[str, Any]] | None = None,
        info_files: Mapping[str, Any] | None = None,
        message: str | None = None,
        return_kind: str | None = None,
        custom_name: str | None = None,
        expected_custom_name: str | None = None,
    ) -> dict[str, Any]:
        self._attempt(attempt)
        body = self._wire_body(
            RunnerTaskCommitRequestV1,
            {
                "claim_id": claim_id,
                "attempt": attempt,
                "expected_status": expected_status,
                "results": list(results or ()),
                "info_files": info_files,
                "message": message,
                "return_kind": return_kind,
                "custom_name": custom_name,
                "expected_custom_name": expected_custom_name,
            },
            operation="task commit request",
            exclude_none=False,
        )
        path = self._protocol(task_commit_path, task_id)
        response = self._require_object(
            await self._request("POST", path, body, retry_safe=True),
            operation="task commit",
        )
        return self._wire_dto(
            RunnerTaskCommitResponseV1,
            response,
            operation="task commit response",
        ).model_dump(mode="json")
