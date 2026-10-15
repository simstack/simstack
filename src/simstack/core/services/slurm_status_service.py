import asyncio
import logging
from datetime import UTC, datetime

from simstack.core.context import context
from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.services.base_service import BaseService
from simstack.core.runner_execution import node_registry_from_task
from simstack.models.parameters import Resource
from simstack.runner_runtime.slurm import get_job_info
from simstack.runner_protocol import (
    DEFAULT_RUNNER_LEASE_SECONDS,
    MAX_RUNNER_LEASE_SECONDS,
    RunnerTaskStatus,
)

logger = logging.getLogger("NodeRunner")


class SlurmStatusService(BaseService):
    """Reconcile server-owned active tasks with the local Slurm scheduler."""

    def __init__(self, resource: Resource, interval: int, max_concurrent: int) -> None:
        if isinstance(max_concurrent, bool) or max_concurrent < 1:
            raise ValueError("Slurm status max_concurrent must be positive")
        super().__init__("SlurmStatus", resource, interval)
        self._resource_name = str(resource)
        requested_lease = max(interval * 3, DEFAULT_RUNNER_LEASE_SECONDS)
        if requested_lease > MAX_RUNNER_LEASE_SECONDS:
            raise ValueError(
                "Slurm status interval cannot be renewed within the protocol lease bound"
            )
        self._lease_seconds = requested_lease
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def execute(self) -> None:
        active_claims = await context.runner_api.list_active_tasks(
            statuses=(
                RunnerTaskStatus.RUNNING,
                RunnerTaskStatus.SLURM_QUEUED,
                RunnerTaskStatus.SLURM_RUNNING,
            )
        )

        results = await asyncio.gather(
            *(self._reconcile(active_claim) for active_claim in active_claims),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error(
                    "Failed to reconcile one active Slurm claim",
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def _reconcile(self, active_claim: dict[str, object]) -> None:
        async with self._semaphore:
            claim_id = active_claim.get("claim_id")
            execution_token = active_claim.get("execution_token")
            execution_token_expires_at = active_claim.get("execution_token_expires_at")
            attempt = active_claim.get("attempt")
            task_payload = active_claim.get("task")
            if (
                type(attempt) is not int
                or attempt < 1
                or not isinstance(claim_id, str)
                or not claim_id.strip()
                or not isinstance(execution_token, str)
                or not execution_token.strip()
                or not isinstance(execution_token_expires_at, str)
                or not execution_token_expires_at.strip()
                or not isinstance(task_payload, dict)
            ):
                raise RuntimeError("Runner active task envelope is malformed")
            execution_api = context.runner_api.execution_client(execution_token)

            task = node_registry_from_task(task_payload)
            if task.job_id is None:
                return

            task_id = str(task.id)
            await execution_api.renew_lease(
                task_id,
                claim_id=claim_id,
                attempt=attempt,
                lease_seconds=self._lease_seconds,
            )

            slurm_info = await asyncio.to_thread(
                get_job_info,
                task.job_id,
                task.id,
                Resource(value=self._resource_name),
            )
            current_status = (
                task.status.value
                if isinstance(task.status, TaskStatus)
                else str(task.status)
            )
            if slurm_info is not None:
                await execution_api.update_task_status(
                    task_id,
                    claim_id=claim_id,
                    attempt=attempt,
                    expected_status=current_status,
                    status=current_status,
                    observed_at=slurm_info.updated.isoformat(),
                    job_id=task.job_id,
                    slurm_info={
                        "job_id": slurm_info.job_id,
                        "name": slurm_info.name,
                        "user": slurm_info.user,
                        "code": slurm_info.code,
                        "time": slurm_info.time,
                        "nodes": list(slurm_info.nodes),
                        "updated": slurm_info.updated.isoformat(),
                    },
                )
                return

            if current_status in {
                RunnerTaskStatus.RUNNING,
                RunnerTaskStatus.SLURM_QUEUED,
                RunnerTaskStatus.SLURM_RUNNING,
            }:
                await execution_api.update_task_status(
                    task_id,
                    claim_id=claim_id,
                    attempt=attempt,
                    expected_status=current_status,
                    status=RunnerTaskStatus.TIMEOUT,
                    observed_at=datetime.now(UTC).isoformat(),
                    message="Slurm job is no longer present in squeue",
                )
