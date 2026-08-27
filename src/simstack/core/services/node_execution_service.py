import asyncio
import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Any

from odmantic import Model

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_database
from simstack.core.node_claim import claim_submitted_node
from simstack.core.run_docker import run_docker
from simstack.core.services.base_service import BaseService
from simstack.core.simstack_result import SimstackResult
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.util.sanitized_output import sanitized_command, sanitized_tail

logger = logging.getLogger("NodeRunner")


@dataclass(frozen=True)
class NodeExecutionOutcome:
    success: bool
    return_kind: str


def _return_kind(result: Any, registry_entry: NodeRegistry) -> str:
    if isinstance(result, bool):
        return "bool"
    if isinstance(result, SimstackResult):
        return "multiple"
    if isinstance(result, Model):
        return "model"
    if result is None and registry_entry.status == TaskStatus.COMPLETED:
        if len(registry_entry.results_references) > 1:
            return "multiple"
        if registry_entry.results_references:
            return "model"
        return "bool"
    return "none"


async def run_node_from_registry_with_outcome(
    registry_entry: NodeRegistry,
) -> NodeExecutionOutcome:
    """
    Executes a node task from the provided registry entry and updates its status
    based on the success or failure of execution.
    This always leads to local execution.

    Parameters:
        registry_entry (NodeRegistry): The registry entry containing information
        about the node execution target.

    Returns:
        bool: True if the node execution completes successfully, False otherwise.

    Raises:
        AssertionError: If the registry entry associated with the node becomes None after execution.

    """
    # Create the node from the registry entry
    node = await node_from_database(registry_entry)
    if not node:
        error = registry_entry.error or sanitized_tail(
            f"Failed to create node {registry_entry.func_mapping}",
            getattr(context.config, "connection_string", None),
        )
        logger.error(
            "Failed to create node from registry entry task_id: %s on "
            "resource %s: %s",
            registry_entry.id,
            context.config.resource,
            error,
        )
        registry_entry.status = TaskStatus.FAILED
        registry_entry.error = error
        registry_entry.return_kind = "exception"
        await context.db.save(registry_entry)
        return NodeExecutionOutcome(False, "exception")
    registry_entry = node.registry_entry  # it may have changed
    assert registry_entry is not None
    if (
        node.status == TaskStatus.RETRIEVED
        or node.status == TaskStatus.SUBMITTED
        or node.status == TaskStatus.SLURM_QUEUED
    ) or (
        node.status == TaskStatus.COMPLETED and registry_entry.parameters.force_rerun
    ):
        result = await node.execute_node_locally()
    else:
        result = None
        logger.info(
            f"task_id: {registry_entry.id} skipping task: {registry_entry.name} with status {registry_entry.status}"
        )
    return_kind = getattr(node, "_execution_return_kind", None)
    if return_kind is None:
        return_kind = _return_kind(result, registry_entry)
    return NodeExecutionOutcome(node.status == TaskStatus.COMPLETED, return_kind)


async def run_node_from_registry(registry_entry: NodeRegistry) -> bool:
    """Execute a registry entry while preserving the existing boolean API."""
    return (await run_node_from_registry_with_outcome(registry_entry)).success


class NodeExecutionService(BaseService):
    def __init__(
        self,
        resource: Resource,
        interval: int,
        max_concurrent: int,
        shutdown_event: asyncio.Event | None,
        detach: bool = True,
        is_default: bool = False,
    ) -> None:
        super().__init__(
            "JobPolling", resource, interval, shutdown_event=shutdown_event
        )
        self._resource_name = str(resource)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running_tasks: set[asyncio.Task[bool]] = set()
        self._started = False
        self._detach = detach
        self._is_default = is_default

    async def run_node(self, registry_entry: NodeRegistry) -> bool:
        """Run a single node by its ID from the database"""

        await self.write_node_event(RunnerEventEnum.NODE_STARTED, registry_entry.id)
        try:

            queue = (
                registry_entry.parameters.queue
                if hasattr(registry_entry.parameters, "queue")
                else "default"
            )
            if queue is None:
                logger.error(
                    f"Queue parameter not found for task_id: {registry_entry.id}"
                )
                return False

            logger.info(
                f"Running node task_id: {registry_entry.id} on resource {context.config.resource} with {queue} queue and docker: {registry_entry.parameters.in_docker} "
            )

            if queue == "slurm-docker":
                registry_entry.parameters.in_docker = True
                queue = "slurm-queue"

            if queue == "slurm-queue":
                return await submit_node(registry_entry)

            if queue == "docker":
                registry_entry.parameters.in_docker = True
                queue = "default"

            if queue == "default" and registry_entry.parameters.in_docker:
                return await run_docker(registry_entry)

            if queue == "default":
                if self._detach:
                    # Spawn independent process that survives when the runner dies
                    project_root = str(context.config.project_root)
                    cmd = [
                        "uv",
                        "run",
                        "--directory",
                        project_root,
                        "run_node",
                        "--node-id",
                        str(registry_entry.id),
                        "--resource",
                        str(self._resource_name),
                    ]

                    # Use platform specific flags to ensure the process survives if runner is killed
                    creationflags = 0
                    if platform.system() == "Windows":
                        create_new_process_group = getattr(
                            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                        )
                        detached_process = getattr(subprocess, "DETACHED_PROCESS", 0)
                        creationflags = create_new_process_group | detached_process

                    python_path_entries = [project_root]
                    python_path_entries.extend(
                        str(path)
                        for path in (
                            getattr(context.config, "python_paths", None) or []
                        )
                    )
                    if os.environ.get("PYTHONPATH"):
                        python_path_entries.append(os.environ["PYTHONPATH"])
                    child_python_path = os.pathsep.join(
                        dict.fromkeys(
                            path for path in python_path_entries if path
                        )
                    )

                    try:
                        process = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                            creationflags=creationflags,
                            start_new_session=True
                            if platform.system() != "Windows"
                            else False,
                            env={
                                **os.environ,
                                "PYTHONPATH": child_python_path,
                                **(
                                    {
                                        "SIMSTACK_DB_CONNECTION_STRING": str(
                                            context.config.connection_string
                                        )
                                    }
                                    if getattr(
                                        context.config,
                                        "connection_string",
                                        None,
                                    )
                                    is not None
                                    else {}
                                ),
                                **(
                                    {
                                        "SIMSTACK_DB_DATABASE": str(
                                            context.config.db_name
                                        )
                                    }
                                    if getattr(context.config, "db_name", None)
                                    is not None
                                    else {}
                                ),
                            },
                        )
                    except Exception as exc:
                        error = sanitized_tail(
                            str(exc),
                            getattr(context.config, "connection_string", None),
                        )
                        logger.error(
                            "Failed to spawn detached process for task_id: %s. "
                            "Command: %s. Error: %s",
                            registry_entry.id,
                            " ".join(
                                sanitized_command(
                                    cmd,
                                    getattr(
                                        context.config, "connection_string", None
                                    ),
                                )
                            ),
                            error,
                        )
                        raise

                    logger.info(
                        f"Spawned detached process for task_id: {registry_entry.id} with PID: {process.pid}"
                    )
                    return True
                else:
                    return await run_node_from_registry(registry_entry)
            else:
                logger.error(
                    f"Queue {queue} not supported {registry_entry.name} for task_id: {registry_entry.id}"
                )
                raise RuntimeError(f"Queue {queue} not supported for task_id: {registry_entry.id}")

        except (Exception, SystemExit) as exc:
            error = sanitized_tail(
                str(exc), getattr(context.config, "connection_string", None)
            ) or type(exc).__name__
            logger.error(
                "Error running node task_id: %s on resource %s: %s",
                registry_entry.id,
                context.config.resource,
                error,
            )
            if registry_entry:
                registry_entry.status = TaskStatus.FAILED
                registry_entry.error = error
                registry_entry.return_kind = "exception"
                await context.db.save(registry_entry)
            return False

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

        # Load tasks
        registry_entry_list = await context.db.load_waiting_tasks_for_resource(self._resource_name)

        if registry_entry_list:
            logger.info(
                f"Retrieved {len(registry_entry_list)} tasks for {self._resource_name}"
            )
            for entry in registry_entry_list:
                if not await claim_submitted_node(entry):
                    continue
                task = asyncio.create_task(self._run_with_semaphore(entry))
                self._running_tasks.add(task)

    async def _run_with_semaphore(self, entry: NodeRegistry) -> bool:
        async with self._semaphore:
            return await self.run_node(entry)
