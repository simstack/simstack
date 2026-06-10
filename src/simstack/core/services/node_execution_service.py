import asyncio
import logging
import platform
import subprocess

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_database
from simstack.core.node_claim import claim_submitted_node
from simstack.core.run_docker import run_docker
from simstack.core.services.base_service import BaseService
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum

logger = logging.getLogger("NodeRunner")


async def run_node_from_registry(registry_entry: NodeRegistry) -> bool:
    # Create the node from the registry entry
    node = await node_from_database(registry_entry)
    if not node:
        logger.error(
            f"Failed to create node from registry entry task_id: {registry_entry.id} on resource {context.config.resource}"
        )
        registry_entry.status = TaskStatus.FAILED
        await context.db.save(registry_entry)
        return False
    registry_entry = node.registry_entry  # it may have changed
    assert registry_entry is not None
    if (
        node.status == TaskStatus.RETRIEVED
        or node.status == TaskStatus.SUBMITTED
        or node.status == TaskStatus.SLURM_QUEUED
    ) or (
        node.status == TaskStatus.COMPLETED and registry_entry.parameters.force_rerun
    ):
        await node.execute_node_locally()
    else:
        logger.info(
            f"task_id: {registry_entry.id} skipping task: {registry_entry.name} with status {registry_entry.status}"
        )
    return bool(node.status == TaskStatus.COMPLETED)


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
            "^NodeExecutionService", resource, interval, shutdown_event=shutdown_event
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
            logger.info(
                f"Running node task_id: {registry_entry.id} on resource {context.config.resource} with status {registry_entry.status}"
            )
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

            if queue == "slurm-queue":
                await submit_node(registry_entry)
                return True
            elif queue == "docker":
                await run_docker(registry_entry)
                return True

            elif queue == "default":
                if self._detach:
                    # Spawn independent process that survives when the runner dies
                    cmd = [
                        "uv",
                        "run",
                        "--directory",
                        str(context.config.project_root),
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

                    try:
                        process = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                            creationflags=creationflags,
                            start_new_session=True
                            if platform.system() != "Windows"
                            else False,
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
                    return True
                else:
                    return await run_node_from_registry(registry_entry)
            else:
                logger.error(
                    f"Queue {queue} not supported for task_id: {registry_entry.id}"
                )
                return False

        except Exception as e:
            logger.exception(
                f"Error running node task_id: {registry_entry.id} on resource {context.config.resource} : {str(e)}"
            )
            if registry_entry:
                registry_entry.status = TaskStatus.FAILED
                await context.db.save(registry_entry)
            return False

    def _cleanup_running_tasks(self) -> None:
        """Clean up the completed tasks from the running tasks set"""
        completed_tasks = {task for task in self._running_tasks if task.done()}
        for task in completed_tasks:
            try:
                # Use result() or exception() to avoid warning if task finished with error
                if not task.cancelled():
                    task.result()
            except Exception as e:
                logger.exception(f"Task completed with error: {e}")
            self._running_tasks.remove(task)

    async def execute(self) -> None:
        if not self._started:
            await self.write_resource_event(RunnerEventEnum.RUNNER_STARTED)
            self._started = True

        # Clean up the completed tasks
        self._cleanup_running_tasks()

        # Periodically check for any missed tasks (safety net)
        registry_entry_list = await context.db.load_waiting_tasks_for_resource(
            self._resource_name
        )

        if registry_entry_list:
            logger.info(
                f"Retrieved {len(registry_entry_list)} tasks for {self._resource_name}"
            )
            for entry in registry_entry_list:
                if not await claim_submitted_node(entry):
                    continue
                task = asyncio.create_task(self._run_with_semaphore(entry))
                self._running_tasks.add(task)

    async def _run_watch_loop(self) -> None:
        """Watch for new tasks using MongoDB change streams"""
        logger.info(f"Starting watch loop for resource {self._resource_name}")
        try:
            collection = context.db.get_collection(NodeRegistry)
            pipeline = [
                {
                    "$match": {
                        "operationType": {"$in": ["insert", "replace", "update"]},
                        "fullDocument.status": TaskStatus.SUBMITTED.value,
                        "fullDocument.parameters.resource.value": self._resource_name,
                    }
                }
            ]

            async with collection.watch(
                pipeline, full_document="updateLookup"
            ) as stream:
                async for change in stream:
                    if self._stop_event.is_set():
                        break

                    full_doc = change.get("fullDocument")
                    if not full_doc:
                        continue

                    # Convert raw doc to NodeRegistry instance
                    # odmantic doesn't have a direct from_dict, but we can use the engine's internal logic
                    # or just load it by ID to be safe and use existing logic.
                    task_id = full_doc.get("_id")
                    if not task_id:
                        continue

                    entry = await context.db.load_task_by_id(task_id)
                    if not entry:
                        continue

                    if not await claim_submitted_node(entry):
                        continue

                    logger.info(
                        f"Change stream triggered task_id: {entry.id} for {self._resource_name}"
                    )
                    task = asyncio.create_task(self._run_with_semaphore(entry))
                    self._running_tasks.add(task)

                    # Periodically clean up completed tasks from the set
                    self._cleanup_running_tasks()

        except Exception as e:
            if self._stop_event.is_set():
                return
            logger.error(f"Error in watch loop: {e}")
            # If watch fails, we might want to fall back or just let it die and be restarted if BaseService handles it
            # But here we are in a separate task.
            await asyncio.sleep(5)
            # Restart watch loop
            self._watch_task = asyncio.create_task(self._run_watch_loop())

    def start(self):
        # We still want the periodic execute for cleanup and initial load
        task = super().start()
        # Also start the watch loop
        self._watch_task = asyncio.create_task(self._run_watch_loop())
        return task

    async def stop(self):
        await super().stop()
        if hasattr(self, "_watch_task"):
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

    async def _run_with_semaphore(self, entry: NodeRegistry) -> bool:
        async with self._semaphore:
            return await self.run_node(entry)
