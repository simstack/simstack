"""Supervisor-token runner API client."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from simstack.runner_protocol import (
    BOOTSTRAP_ROUTE,
    DEFAULT_RUNNER_LEASE_SECONDS,
    HEARTBEAT_ROUTE,
    RunnerHeartbeatRequestV1,
    RunnerHeartbeatResponseV1,
    RunnerTaskClaimRequestV1,
    RunnerTaskStatus,
    TASK_CLAIM_ROUTE,
    active_tasks_path,
    validate_runner_active_tasks_v1,
    validate_runner_bootstrap_v1,
)

from .base import BaseRunnerClient
from .models import ClaimedTask
from .parsing import claimed_task
from .errors import RunnerApiError
from simstack.runner_protocol.dto.workflows import (
    RunnerWorkflowClaimRequestV1,
    RunnerWorkflowClaimV1,
    RunnerWorkflowStatusRequestV1,
)

if TYPE_CHECKING:
    from .execution import ExecutionRunnerClient


class SupervisorRunnerClient(BaseRunnerClient):
    """Only operations authorized for a resource-scoped supervisor token."""

    def execution_client(self, execution_token: str) -> "ExecutionRunnerClient":
        from .execution import ExecutionRunnerClient

        client = ExecutionRunnerClient(
            server_url=self.server_url,
            runner_token=execution_token,
            timeout_seconds=self.timeout_seconds,
            retry_attempts=self.retry_attempts,
            retry_backoff_seconds=self.retry_backoff_seconds,
        )
        self._copy_payload_limit_to(client)
        return client

    async def bootstrap(self) -> dict[str, Any]:
        payload = self._require_object(
            await self._request("GET", BOOTSTRAP_ROUTE, retry_safe=True),
            operation="bootstrap",
        )
        parsed = self._protocol(
            validate_runner_bootstrap_v1,
            payload,
            operation="bootstrap response",
        )
        self._configure_capabilities(
            parsed["capabilities"], operation="bootstrap response"
        )
        return parsed

    async def register_program_policy(self, task_id: str, *, policy):
        from simstack.runner_protocol.paths import task_program_policy_path
        from simstack.runner_protocol.dto.programs import (
            RunnerProgramPolicyRequestV1,
            RunnerProgramPolicyResponseV1,
        )

        body = self._wire_body(
            RunnerProgramPolicyRequestV1,
            policy.model_dump(mode="json"),
            operation="named-program policy",
        )
        response = self._require_object(
            await self._request(
                "POST", task_program_policy_path(task_id), body, retry_safe=True
            ),
            operation="named-program policy",
        )
        result = self._wire_dto(
            RunnerProgramPolicyResponseV1,
            response,
            operation="named-program policy acknowledgement",
        )
        if (result.task_id, result.claim_id, result.attempt, result.policy_revision) != (
            task_id, policy.claim_id, policy.attempt, policy.fingerprint()
        ):
            raise RunnerApiError(
                "Named-program policy acknowledgement differs from the active task claim"
            )
        return result

    async def pending_workflow_installations(self) -> list[dict[str, Any]]:
        from simstack.runner_protocol.paths import RunnerRoute

        response = self._require_object(
            await self._request("GET", RunnerRoute.WORKFLOW_PENDING, retry_safe=True),
            operation="workflow installations",
        )
        installations = response.get("installations")
        if not isinstance(installations, list) or any(
            not isinstance(item, dict) for item in installations
        ):
            raise RunnerApiError(
                "Workflow installations response requires an installations array"
            )
        return installations

    async def claim_workflow_installation(
        self, installation_id: str, *, request_id: str, lease_seconds: int = 600
    ) -> dict[str, Any]:
        from simstack.runner_protocol.paths import workflow_installation_path

        body = self._wire_body(
            RunnerWorkflowClaimRequestV1,
            {
                "request_id": request_id,
                "lease_seconds": lease_seconds,
            },
            operation="workflow installation claim request",
        )
        response = self._require_object(
            await self._request(
                "POST",
                workflow_installation_path(installation_id, "claim"),
                body,
                retry_safe=True,
            ),
            operation="workflow installation claim",
        )
        identity = {
            "installation_id": response.get("installation_id"),
            "package_id": response.get("package_id"),
            "revision": response.get("workflow_revision"),
            "runtime_profile": response.get("runtime_profile"),
            "manifest": response.get("manifest"),
            "claim_id": response.get("claim_id"),
            "attempt": response.get("attempt"),
            "lease_expires_at": response.get("lease_expires_at"),
        }
        parsed = self._wire_dto(
            RunnerWorkflowClaimV1,
            identity,
            operation="workflow installation claim response",
        )
        if parsed.installation_id != installation_id:
            raise RunnerApiError(
                "Workflow installation claim response has a different installation identity"
            )
        return response

    async def report_workflow_installation(
        self,
        installation_id: str,
        *,
        revision: str,
        claim_id: str,
        attempt: int,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        from simstack.runner_protocol.paths import workflow_installation_path

        body = self._wire_body(
            RunnerWorkflowStatusRequestV1,
            {
                "workflow_revision": revision,
                "claim_id": claim_id,
                "attempt": attempt,
                "status": status,
                "error": error,
            },
            operation="workflow installation status request",
        )
        return self._require_object(
            await self._request(
                "POST",
                workflow_installation_path(installation_id, "status"),
                body,
                retry_safe=True,
            ),
            operation="workflow installation status",
        )

    async def claim_task(
        self,
        *,
        lease_seconds: int = DEFAULT_RUNNER_LEASE_SECONDS,
    ) -> ClaimedTask | None:
        request_id = uuid4().hex
        body = self._wire_body(
            RunnerTaskClaimRequestV1,
            {"lease_seconds": lease_seconds, "request_id": request_id},
            operation="claim request",
        )
        payload = await self._request(
            "POST",
            TASK_CLAIM_ROUTE,
            body,
            retry_safe=True,
        )
        if payload is None:
            return None
        return claimed_task(self, payload, operation="claim")

    async def list_active_tasks(
        self,
        *,
        statuses: Sequence[str] = (
            RunnerTaskStatus.RUNNING,
            RunnerTaskStatus.SLURM_QUEUED,
        ),
    ) -> list[dict[str, Any]]:
        path = self._protocol(active_tasks_path, list(statuses))
        payload = self._require_object(
            await self._request("GET", path, retry_safe=True),
            operation="active task listing",
        )
        parsed = self._protocol(
            validate_runner_active_tasks_v1,
            payload,
            operation="active task listing",
        )
        return parsed["tasks"]

    async def heartbeat(
        self,
        *,
        hostname: str | None = None,
        pid: int | None = None,
        message: str | None = None,
        git_status: Sequence[str] | None = None,
        runner_version: str | None = None,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "hostname": hostname,
            "pid": pid,
            "message": message,
            "git_status": list(git_status or []),
            "runner_version": runner_version,
        }
        body = self._wire_body(
            RunnerHeartbeatRequestV1,
            values,
            operation="runner heartbeat request",
        )
        response = self._require_object(
            await self._request("PUT", HEARTBEAT_ROUTE, body),
            operation="runner heartbeat",
        )
        return self._wire_dto(
            RunnerHeartbeatResponseV1,
            response,
            operation="runner heartbeat response",
        ).model_dump(mode="json")
