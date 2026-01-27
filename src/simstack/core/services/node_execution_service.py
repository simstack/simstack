import asyncio
import logging
import platform
import subprocess

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_database
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.core.services.base_service import BaseService

logger = logging.getLogger("NodeRunner")

async def run_node_from_registry(registry_entry: NodeRegistry):
    # Create the node from the registry entry
    node = await node_from_database(registry_entry)
    if not node:
        logger.error(
            f"Failed to create node from registry entry task_id: {registry_entry.id} on resource {context.config.resource}")
        registry_entry.status = TaskStatus.FAILED
        await context.db.save(registry_entry)
        return False
    registry_entry = node.registry_entry  # it may have changed
    if node.status == TaskStatus.SUBMITTED or node.status == TaskStatus.SLURM_QUEUED or node.status == TaskStatus.SLURM_QUEUED:
        await node.execute_node_locally()
    else:
        logger.info(
            f"task_id: {registry_entry.id} skipping task: {registry_entry.name} with status {registry_entry.status}")

    return node.status == TaskStatus.COMPLETED

class NodeExecutionService(BaseService):
    def __init__(self, resource: Resource, interval, max_concurrent, shutdown_event, detach: bool = True):
        super().__init__("JobPolling", resource, interval, shutdown_event=shutdown_event)
        self._resource_name = str(resource)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running_tasks = set()
        self._started = False
        self._detach = detach

    async def run_node(self, registry_entry: NodeRegistry):
        """Run a single node by its ID from the database"""

        await self.write_node_event(RunnerEventEnum.NODE_STARTED, registry_entry.id)
        try:

            logger.info(f"Running node task_id: {registry_entry.id} on resource {context.config.resource}")
            if (
                    hasattr(registry_entry.parameters, "queue")
                    and registry_entry.parameters.queue == "slurm-queue"
            ):
                await submit_node(registry_entry)
            elif self._detach:
                # Spawn independent process that survives when the runner dies
                cmd = [
                    "uv", "run", "run_node", "--node-id",
                    str(registry_entry.id),
                    "--resource", str(self._resource_name)
                ]

                # Use platform specific flags to ensure the process survives if runner is killed
                creationflags = 0
                if platform.system() == "Windows":
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=creationflags,
                    start_new_session=True if platform.system() != "Windows" else False
                )
                logger.info(f"Spawned detached process for task_id: {registry_entry.id} with PID: {process.pid}")
                return True
            else:
                return await run_node_from_registry(registry_entry)

        except Exception as e:
            logger.exception(
                f"Error running node task_id: {registry_entry.id} on resource {context.config.resource} : {str(e)}"
            )
            if registry_entry:
                registry_entry.status = TaskStatus.FAILED
                await context.db.save(registry_entry)
            return False

    async def execute(self):
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
            logger.info(f"Retrieved {len(registry_entry_list)} tasks for {self._resource_name}")
            for entry in registry_entry_list:

                task = asyncio.create_task(self._run_with_semaphore(entry))
                self._running_tasks.add(task)

    async def _run_with_semaphore(self, entry):
        async with self._semaphore:
            return await self.run_node(entry)
