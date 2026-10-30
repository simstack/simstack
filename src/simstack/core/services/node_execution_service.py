import asyncio
import logging
import platform
import subprocess
from pathlib import Path

from simstack.core.context import context
from simstack.core.run_docker import run_docker
from simstack.core.runner_execution import node_registry_from_task
from simstack.core.runner_execution.config import lease_seconds
from simstack.core.services.base_service import BaseService
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Queue, Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.runner_api import (
    ClaimedTask,
    ExecutionRunnerClient,
    RunnerApiError,
)
from simstack.runner_runtime.logging import flush_runner_api_logs
from simstack.runner_runtime.processes import supervise_worker, worker_failure_diagnostic
from simstack.runner_runtime.environment import (
    sanitized_runner_environment,
    worker_environment_allowlist_value,
)

logger = logging.getLogger("NodeRunner")


class NodeExecutionService(BaseService):
    def __init__(
        self,
        resource: Resource,
        interval: int,
        max_concurrent: int,
        shutdown_event: asyncio.Event | None,
        is_default: bool = False,
    ) -> None:
        super().__init__(
            "JobPolling", resource, interval, shutdown_event=shutdown_event
        )
        self._resource_name = str(resource)
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running_tasks: set[asyncio.Task[bool]] = set()
        self._started = False
        self._is_default = is_default
        self._lease_seconds = lease_seconds()

    async def run_node(
        self,
        registry_entry: NodeRegistry,
        *,
        claim_id: str,
        attempt: int,
        execution_api: ExecutionRunnerClient,
    ) -> bool:
        """Dispatch a task claimed atomically by the server."""

        await self.write_node_event(
            RunnerEventEnum.NODE_STARTED,
            registry_entry.id,
            execution_api=execution_api,
        )
        try:
            logger.info(
                f"Running node task_id: {registry_entry.id} on resource {context.config.resource} with status {registry_entry.status}"
            )
            await self._prepare_package_with_lease(
                registry_entry, claim_id=claim_id, attempt=attempt,
                execution_api=execution_api,
            )
            from simstack.runner_runtime.programs import publish_task_program_policy
            await publish_task_program_policy(context.runner_api, context.runner_adapter,
                task_id=str(registry_entry.id), claim_id=claim_id, attempt=attempt)
            queue = registry_entry.parameters.queue

            if queue == Queue.SLURM_QUEUE:
                return await submit_node(
                    registry_entry,
                    claim_id=claim_id,
                    attempt=attempt,
                    runner_api=execution_api,
                )
            elif registry_entry.parameters.in_docker:
                return await run_docker(
                    registry_entry,
                    claim_id=claim_id,
                    attempt=attempt,
                    lease_seconds=self._lease_seconds,
                    runner_api=execution_api,
                )

            elif queue == Queue.DEFAULT:
                profile = getattr(registry_entry, "runtime_profile", None)
                adapter = context.runner_adapter.local(registry_entry.name, **({"runtime_profile": profile} if profile else {}))
                environment_allowlist = adapter.boundary.environment_allowlist
                from simstack.runner_runtime.workflow_installation import packaged_worker_command

                cmd = list(packaged_worker_command(adapter.guarded_worker_command, registry_entry, workdir=Path(context.config.workdir)))
                cmd.extend(
                    (
                        "--node-id",
                        str(registry_entry.id),
                        "--claim-id",
                        claim_id,
                        "--attempt",
                        str(attempt),
                        "--resource",
                        str(self._resource_name),
                        "--project-root",
                        str(context.config.project_root),
                    )
                )

                # Use platform specific flags to ensure the process survives if runner is killed
                creationflags = 0
                if platform.system() == "Windows":
                    create_new_process_group = getattr(
                        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                    )
                    detached_process = getattr(subprocess, "DETACHED_PROCESS", 0)
                    creationflags = create_new_process_group | detached_process

                try:
                    child_environment = sanitized_runner_environment(
                        {
                            "SIMSTACK_SERVER_URL": execution_api.server_url,
                            "SIMSTACK_RUNNER_TOKEN": execution_api.runner_token,
                            "SIMSTACK_TASK_ID": str(registry_entry.id),
                            "SIMSTACK_TASK_CLAIM_ID": claim_id,
                            "SIMSTACK_TASK_ATTEMPT": str(attempt),
                            "SIMSTACK_RUNNER_RESOURCE": str(self._resource_name),
                            "SIMSTACK_PROJECT_ROOT": str(context.config.project_root),
                            "SIMSTACK_RUNNER_WORKDIR": str(context.config.workdir),
                            "SIMSTACK_WORKER_ENVIRONMENT_ALLOWLIST": (
                                worker_environment_allowlist_value(
                                    environment_allowlist
                                )
                            ),
                        },
                        allowed_environment_names=environment_allowlist,
                        workflow_bootstrap=bool(getattr(registry_entry, "workflow_revision", None)),
                    )
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        creationflags=creationflags,
                        start_new_session=True
                        if platform.system() != "Windows"
                        else False,
                        env=child_environment,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to spawn detached process for task id: {registry_entry.id}. task_id: {registry_entry.id}. "
                        f"Command: {' '.join(cmd)}. Error: {str(e)}"
                    )
                    raise

                logger.info(
                    f"Spawned detached process for task_id: {registry_entry.id} with PID: {process.pid}"
                )
                result = await self._wait_for_process_with_lease(
                    process, task_id=str(registry_entry.id), claim_id=claim_id,
                    attempt=attempt, execution_api=execution_api,
                )
                return_code = result.returncode
                if return_code != 0:
                    diagnostic = worker_failure_diagnostic(
                        result.stdout,
                        result.stderr,
                        secrets=(execution_api.runner_token,),
                    )
                    detail = (
                        f". Bounded worker diagnostic:\n{diagnostic}"
                        if diagnostic
                        else ""
                    )
                    raise RuntimeError(
                        f"Detached task process exited with code {return_code}{detail}"
                    )
                return True
            else:
                logger.error(
                    f"Queue {queue} not supported {registry_entry.name} for task_id: {registry_entry.id}"
                )
                raise RuntimeError(
                    f"Queue {queue} not supported for task_id: {registry_entry.id}"
                )

        except Exception as e:
            logger.exception(
                f"Error running node task_id: {registry_entry.id} on resource {context.config.resource} : {str(e)}"
            )
            try:
                await asyncio.to_thread(flush_runner_api_logs)
                await execution_api.fail_task(
                    str(registry_entry.id),
                    claim_id=claim_id,
                    attempt=attempt,
                    error=type(e).__name__,
                    message=str(e),
                )
            except RunnerApiError as report_error:
                if report_error.status_code != 409:
                    raise
                logger.info(
                    "Task %s was already finalized while reporting worker failure",
                    registry_entry.id,
                )
            return False

    async def _prepare_package_with_lease(
        self, registry_entry, *, claim_id, attempt, execution_api,
    ) -> None:
        from simstack.runner_runtime.workflow_installation import ensure_claimed_package_runtime

        if all(getattr(registry_entry, field, None) is None for field in (
            "workflow_installation_id", "workflow_package", "workflow_revision", "runtime_profile",
        )):
            return

        async def renew():
            return await execution_api.renew_lease(
                str(registry_entry.id), claim_id=claim_id, attempt=attempt,
                lease_seconds=self._lease_seconds,
            )

        async def renew_loop():
            while True:
                await asyncio.sleep(self._lease_seconds / 3)
                await renew()

        preparation = asyncio.create_task(ensure_claimed_package_runtime(
            execution_api, registry_entry, claim_id=claim_id, attempt=attempt,
            workdir=Path(context.config.workdir), adapter=context.runner_adapter,
        ))
        renewal = asyncio.create_task(renew_loop())
        try:
            done, _ = await asyncio.wait((preparation, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            await preparation
            # Preparation may be longer than a claim. Fence dispatch with a
            # fresh API lease even when the cache was already valid.
            await renew()
        finally:
            for task in (preparation, renewal):
                if not task.done():
                    task.cancel()
            await asyncio.gather(preparation, renewal, return_exceptions=True)

    async def _wait_for_process_with_lease(
        self,
        process: asyncio.subprocess.Process,
        *,
        task_id: str,
        claim_id: str,
        attempt: int,
        execution_api: ExecutionRunnerClient,
    ):
        async def renew_lease() -> object:
            return await execution_api.renew_lease(
                task_id, claim_id=claim_id, attempt=attempt,
                lease_seconds=self._lease_seconds,
            )
        return await supervise_worker(
            process, renew_lease=renew_lease,
            renew_every=max(10.0, self._lease_seconds / 3),
        )

    async def execute(self) -> None:
        if not self._started:
            await self.write_resource_event(RunnerEventEnum.RUNNER_STARTED)
            self._started = True

        # Clean up the completed tasks
        completed_tasks = {task for task in self._running_tasks if task.done()}
        for task in completed_tasks:
            try:
                await task
            except Exception as e:
                logger.exception(f"Task completed with error: {e}")
            self._running_tasks.remove(task)

        available_slots = max(0, self._max_concurrent - len(self._running_tasks))
        for _ in range(available_slots):
            claim = await context.runner_api.claim_task(
                lease_seconds=self._lease_seconds
            )
            if claim is None:
                break
            execution_api = context.runner_api.execution_client(claim.execution_token)
            try:
                entry = node_registry_from_task(claim.task)
            except Exception as exc:
                task_id = claim.task.get("id")
                if task_id:
                    await asyncio.to_thread(flush_runner_api_logs)
                    await execution_api.fail_task(
                        str(task_id),
                        claim_id=claim.claim_id,
                        attempt=claim.attempt,
                        error="TaskPayloadValidationError",
                        message=str(exc),
                    )
                raise
            logger.info(
                "Claimed task %s for %s (attempt %s)",
                entry.id,
                self._resource_name,
                claim.attempt,
            )
            task = asyncio.create_task(
                self._run_with_semaphore(entry, claim, execution_api)
            )
            self._running_tasks.add(task)

    async def _run_with_semaphore(
        self,
        entry: NodeRegistry,
        claim: ClaimedTask,
        execution_api: ExecutionRunnerClient,
    ) -> bool:
        async with self._semaphore:
            return await self.run_node(
                entry,
                claim_id=claim.claim_id,
                attempt=claim.attempt,
                execution_api=execution_api,
            )
