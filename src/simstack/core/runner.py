import argparse
import asyncio
import logging
import os
import socket
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import sys
import platform
from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_database
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerType, RunnerEventEnum
from simstack.models.slurm_info import SlurmInfo
from simstack.util.runner_utils import make_git_status_list, get_job_info, clean_slurm_info

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

def get_file_checksum(filepath: Path) -> str:
    """Calculate SHA256 checksum of a file"""
    if not filepath.exists():
        return ""
    hash_sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()

class BaseService(ABC):
    """A managed periodic service that can be stopped gracefully"""

    def __init__(self, name: str, resource: Resource, interval: int, shutdown_event: asyncio.Event = None):
        self._name = name
        self._resource = resource
        self._interval: int = interval
        self._stop_event = asyncio.Event()
        self._shutdown_event = shutdown_event
        self._task = None

        # Common identity attributes
        self._pid = os.getpid()
        self._hostname = socket.gethostname()
        self._username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        self._time_started = datetime.now()

    def _get_uptime_string(self) -> str:
        time_diff = datetime.now() - self._time_started
        days = time_diff.days
        hours, remainder = divmod(time_diff.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m {seconds}s"

    async def write_node_event(self, event: RunnerEventEnum, node_id: ObjectId, message: str = None):
        runner_event = RunnerEvent(
            runner_type=RunnerType.NODE_RUNNER,
            event=event,
            pid=self._pid,
            hostname=self._hostname,
            user=self._username,
            resource=self._resource,
            node_id=node_id,
            message=message,
        )
        await context.db.save(runner_event)

    async def write_resource_event(self, event: RunnerEventEnum, message: str = None):
        git_list = make_git_status_list()
        if event == RunnerEventEnum.ALIVE:
            uptime = self._get_uptime_string()
            message = f"Uptime: {uptime}"

            runner_event = await context.db.find_one(
                RunnerEvent,
                (RunnerEvent.runner_type == RunnerType.RESOURCE_RUNNER)
                & (RunnerEvent.resource == self._resource)
                & (RunnerEvent.event == RunnerEventEnum.ALIVE)
                & (RunnerEvent.pid == self._pid),
            )
            if runner_event:
                runner_event.message = message
                runner_event.timestamp = datetime.now()
                runner_event.git_status = git_list
                await context.db.save(runner_event)
                return

        runner_event = RunnerEvent(
            runner_type=RunnerType.RESOURCE_RUNNER,
            pid=self._pid,
            hostname=self._hostname,
            user=self._username,
            timestamp=datetime.now(),
            git_status=git_list,
            event=event,
            resource=self._resource,
            message=message,
        )
        await context.db.save(runner_event)

    async def _run_loop(self):
        """Internal loop that respects the stop event"""
        logger.info(f"Service {self._name} started.")
        while not self._stop_event.is_set():
            start_time = asyncio.get_event_loop().time()
            try:
                await self.execute()
            except Exception as e:
                logger.exception(f"Error in service {self._name}: {e}")

            # Calculate wait time to maintain interval regardless of execution duration
            elapsed = asyncio.get_event_loop().time() - start_time
            wait_time = max(0, int(self._interval - elapsed))

            try:
                # Wait for interval OR until stop event is set
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_time)
            except asyncio.TimeoutError:
                # Normal path: the interval passed, loop again
                pass

        logger.info(f"Service {self._name} stopped.")

    @abstractmethod
    async def execute(self):
        raise NotImplementedError("Services must implement execute()")

    def start(self):
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_loop())
        return self._task

    async def stop(self):
        self._stop_event.set()
        if self._task:
            await self._task


class RestartService(BaseService, ABC):
    """
    Intermediate abstract class for services that can trigger a runner restart.
    """

    def __init__(self, name: str, resource: Resource, interval: int):
        super().__init__(name, resource, interval)
        self._pid_file = context.config.workdir / "runner.pid"

    async def trigger_restart(self):
        """Spawns an independent process to kill current PID and start new one"""
        current_pid = os.getpid()

        # Write current PID to file so the restarter knows who to kill
        self._pid_file.write_text(str(current_pid))

        
        # Command to restart depends on OS
        # We use sys.executable to ensure we use the same Python interpreter
        # We'll call the runner module again
        script_path = Path(__file__).resolve()


        log_file = script_path.parent / "runner.out"

        args = [sys.executable, str(script_path)] + sys.argv[1:]

        # We need a small helper script or a one-liner that:
        # 1. Waits for this process to die (to avoid port/resource conflicts)
        # 2. Starts the new one
        if platform.system() == "Windows":
            # Windows 'start' command handles detachment well
            # Use ping for a 2-second delay as 'timeout' fails in non-interactive shells
            cmd = f"taskkill /F /PID {current_pid} && ping 127.0.0.1 -n 3 > nul && {' '.join(args)} >> \"{log_file}\" 2>&1"
            subprocess.Popen(cmd, shell=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            # Linux: use a subshell that nohup/disowns
            # Kill, wait, and start
            cmd = f"kill -9 {current_pid} && sleep 2 && {' '.join(args)} >> \"{log_file}\" 2>&1"
            subprocess.Popen(["/bin/bash", "-c", cmd], start_new_session=True)

        # The above commands kill us, so this line might not even log
        logger.info("Restart signal sent. Goodbye!")


class GitUvUpdateService(RestartService):
    """
    Advanced service that performs git pull and uv lock upgrades.
    If changes are detected, it restarts the runner.
    """

    def __init__(self, resource: Resource, interval):
        super().__init__("GitUvUpdate", resource, interval)
        # Resolve project root (assuming we are in src/simstack/core/runner.py)
        self._project_dir = context.config.project_root.resolve(strict=True)
        self._uv_lock_path = context.config.project_root / "uv.lock"

    async def _run_command(self, cmd: list, ignore_error=False) -> str:
        """Run a shell command and return output"""
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self._project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0 and not ignore_error:
            logger.warning(f"Command {' '.join(cmd)} failed: {stderr.decode()}")
        return stdout.decode().strip()
    
    async def execute(self):
        # 1. Git Pull
        # Check checksum before and after pull to see if Git brought a new lockfile
        old_uv_checksum = get_file_checksum(self._uv_lock_path)
        
        # Ensure we don't have local lockfile changes that block the pull

        await self._run_command(["git", "stash"], ignore_error=True)

        git_output = await self._run_command(["git", "pull"])
        git_changed = "Already up to date." not in git_output

        # Clear the stash now that we've pulled
        await self._run_command(["git", "stash", "drop"])

        # Did Git update our lockfile?
        post_git_checksum = get_file_checksum(self._uv_lock_path)
        uv_received_update = old_uv_checksum != post_git_checksum

        # # 2. UV Lock upgrade (The "Producer" check)
        # # check if the simstack package was updated
        # await self._run_command(["uv", "lock", "--upgrade-package", "simstack"])
        # new_uv_checksum = get_file_checksum(self._uv_lock_path)
        # uv_locally_upgraded = post_git_checksum != new_uv_checksum
        uv_locally_upgraded = False # assume that you get the lock always in pyproject.toml

        if uv_locally_upgraded:
            logger.info("Local uv.lock upgrade detected. Syncing environment...")
            await self._run_command(["uv", "sync", "--locked"]) # Update local .venv
            # Removed git add/commit/push as main branch is protected
        
        elif uv_received_update:
            logger.info("New uv.lock received from Git. Syncing environment...")
            # Use --locked because we want to match the committed file exactly
            await self._run_command(["uv", "sync", "--locked"])

        if git_changed or uv_locally_upgraded:
            reason = "Git pull" if git_changed else "Local UV upgrade"
            await self.write_resource_event(RunnerEventEnum.SHUTDOWN, message=reason)
            logger.info(f"Update detected ({reason}). Triggering restart...")
            await self.trigger_restart()


class NodeExecutionService(BaseService):
    def __init__(self, resource: Resource, interval, max_concurrent, shutdown_event, detach: bool = False):
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
        # this is a test
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


class RunnerStatusService(BaseService):
    def __init__(self, resource: Resource, interval):
        super().__init__("RunnerStatus", resource, interval)

    async def execute(self):
        await self.write_resource_event(RunnerEventEnum.ALIVE)


class RunnerCleanupService(BaseService):
    """
    Service that cleans up old RunnerEvent logs for the current resource.
    Removes RESOURCE_RUNNER events older than 30 minutes.
    """

    def __init__(self, resource: Resource, interval: int = 300):
        # Default interval 5 minutes
        super().__init__("RunnerCleanup", resource, interval)

    async def execute(self):
        cutoff_time = datetime.now() - timedelta(minutes=30)
        
        # Find and delete events matching the criteria
        old_events = await context.db.find_many(
            RunnerEvent,
            (RunnerEvent.runner_type == RunnerType.RESOURCE_RUNNER)
            & (RunnerEvent.resource == self._resource)
            & (RunnerEvent.timestamp < cutoff_time)
        )

        if old_events:
            logger.info(f"Cleaning up {len(old_events)} old RunnerEvent logs for resource {self._resource}")
            for event in old_events:
                await context.db.delete(event)


class SlurmStatusService(BaseService):
    def __init__(self, resource: Resource, interval):
        super().__init__("SlurmStatus", resource, interval)
        self._resource_name = str(resource)

    async def execute(self):
        try:

            running_tasks = await context.db.engine.find(
                NodeRegistry,
                (NodeRegistry.status == TaskStatus.RUNNING)
                & (NodeRegistry.parameters.resource == self._resource),
            )
            queued_tasks = await context.db.engine.find(
                NodeRegistry,
                (NodeRegistry.status == TaskStatus.SLURM_QUEUED)
                & (NodeRegistry.parameters.resource == self._resource),
            )
            #logger.info(f"Checking Slurm status for {len(running_tasks)} running jobs on resource {self._resource}")

            for task in list(running_tasks) + list(queued_tasks):
                logger.info(f"Checking Slurm status for task_id: {task.id} with job_id: {task.job_id}")
                if task.job_id is not None:
                    slurm_info = get_job_info(task.job_id, task.id, Resource(value=self._resource_name))
                    logger.info(f"Slurm status for task_id: {task.id}: {task.job_id} {slurm_info}")
                    slurm_entry = await context.db.find_one(SlurmInfo, SlurmInfo.job_id == task.job_id)

                    #logger.info(f"Slurm DB Entry for task_id: {task.id}: {task.job_id} {slurm_info}")

                    if slurm_info:
                        if slurm_entry:
                            slurm_entry.code = slurm_info.code
                            slurm_entry.time = slurm_info.time
                            slurm_entry.updated = datetime.now()
                            await context.db.save(slurm_entry)
                        else:
                            await context.db.save(slurm_info)
                    else:
                        # Logic for finished/timed out jobs
                        check_job = await context.db.engine.find_one(NodeRegistry, NodeRegistry.id == task.id)
                        if slurm_entry:
                            await context.db.delete(slurm_entry)
                        if check_job.status == TaskStatus.RUNNING:
                            task.job_id = None
                            task.status = TaskStatus.TIME_OUT
                            await context.db.save(task)
        except Exception as e:
            logger.exception(f"Error checking Slurm status: {e}")

        #await clean_slurm_info(self._username, self._resource)


class GitRestartService(BaseService):
    """Service that runs an external script to check git/restart the runner"""

    def __init__(self, resource: Resource, interval):
        super().__init__("GitRestart", resource, interval)
        self._resource_name = str(resource)

    def execute(self):
        raise NotImplementedError("GitRestartService must implement execute()")


class StopCheckService(BaseService):
    """Service that checks for STOP file and triggers shutdown"""

    def __init__(self, resource: Resource, interval: int, shutdown_event: asyncio.Event):
        super().__init__("StopCheck", resource, interval, shutdown_event=shutdown_event)

    async def execute(self):
        # Check for STOP file
        for path in [context.config.workdir, context.config.project_root]:
            stop_file = Path(path) / "STOP"
            if stop_file.exists():
                await self.write_resource_event(RunnerEventEnum.SHUTDOWN, message="STOP file found")
                logger.info(f"STOP file found at {stop_file}, signaling shutdown...")
                if self._shutdown_event:
                    self._shutdown_event.set()
                return


class TimeoutRestartService(RestartService):
    """Service that restarts the runner after a specified timeout"""

    def __init__(self, resource: Resource, timeout_minutes: int):
        super().__init__("TimeoutRestart", resource, interval=timeout_minutes * 60)
        self._project_dir = context.config.project_root.resolve(strict=True)
        self._timeout_minutes = timeout_minutes
        self._executed = False

    async def execute(self):
        # Only execute once after the timeout interval
        if not self._executed:
            self._executed = True
            logger.info(f"Timeout of {self._timeout_minutes} minutes reached. Triggering restart...")
            await self.write_resource_event(RunnerEventEnum.SHUTDOWN,
                                            message=f"Timeout restart after {self._timeout_minutes} minutes")
            await self.trigger_restart()


class RunnerManager:
    def __init__(self, resource: Resource, detach: bool = False):
        self._resource = resource
        self._detach = detach
        self._pid = os.getpid()
        self._services = []
        self._shutdown_event = asyncio.Event()
        self._pid_file = context.config.workdir / f"runner_{resource}.pid"

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with given PID is running"""
        try:
            if platform.system() == "Windows":
                # On Windows, os.kill with signal 0 doesn't work, use tasklist
                import subprocess
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True,
                    text=True
                )
                return (str(pid) in result.stdout) if result.stdout  is not None else False
            else:
                # On Unix, send signal 0 to check if process exists
                os.kill(pid, 0)
                return True
        except (OSError, ProcessLookupError):
            return False

    def _check_existing_runner(self):
        """Check if another runner for this resource is already running on this host"""
        if self._pid_file.exists():
            try:
                existing_pid = int(self._pid_file.read_text().strip())
                if existing_pid != self._pid and self._is_process_running(existing_pid):
                    logger.error(
                        f"Another runner for resource '{self._resource}' is already running "
                        f"on this host with PID {existing_pid}. Exiting."
                    )
                    sys.exit(1)
                else:
                    logger.info(
                        f"Stale PID file found for resource '{self._resource}'. "
                        f"Overwriting with current PID {self._pid}."
                    )
            except (ValueError, OSError) as e:
                logger.warning(f"Could not read PID file: {e}. Proceeding with startup.")

    async def stop_all_services(self):
        """Gracefully stop all registered services"""
        logger.info("Stopping all services...")
        await asyncio.gather(*(s.stop() for s in self._services), return_exceptions=True)

    async def run_nodes_for_resource(self, polling_interval=5, max_concurrent=10, timeout=None):
        """Orchestrates multiple independent services"""
        # Check if another runner is already running for this resource
        self._check_existing_runner()

        # Save PID on startup
        self._pid_file.write_text(str(self._pid))

        self._services = [
            NodeExecutionService(self._resource, polling_interval, max_concurrent, self._shutdown_event,
                                 detach=self._detach),
            RunnerStatusService(self._resource, interval=60),
            RunnerCleanupService(self._resource, interval=300),
            SlurmStatusService(self._resource, interval=60),
            GitUvUpdateService(self._resource, interval=60),  # Advanced check every hour
            StopCheckService(self._resource, interval=10, shutdown_event=self._shutdown_event),
        ]

        # Add timeout restart service if timeout is specified
        if timeout is not None:
            self._services.append(TimeoutRestartService(self._resource, timeout))

        # Start all services
        service_tasks = [service.start() for service in self._services]

        try:
            # Wait for either a service task to finish or the shutdown event to be set
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            tasks_to_wait = [*service_tasks, shutdown_task]
            done, pending = await asyncio.wait(
                tasks_to_wait,
                return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            await self.stop_all_services()


async def async_main(args):
    """Async entry point"""
    await context.initialize(resource=args.resource, db_name=args.db_name)
    if args.resource:
        logger.info(f"Setting resource for runner to {args.resource}")
        runner_manager = RunnerManager(context.config.resource, detach=args.detach)
        await runner_manager.run_nodes_for_resource(args.polling_interval, 10, timeout=args.timeout)


def runner_main():
    parser = argparse.ArgumentParser(description="Run nodes for a specific resource")
    parser.add_argument(
        "--resource",
        type=str,
        default="local",
        help="Resource name to process tasks for",
    )

    parser.add_argument(
        "--db-name",
        type=str,
        help="Specify a non-standard database",
    )

    parser.add_argument(
        "--polling-interval",
        type=int,
        default=20,
        help="Interval in seconds between polling for new tasks",
    )

    parser.add_argument(
        "--detach",
        type=lambda x: (str(x).lower() not in ['false', '0', 'no']),
        default=True,
        help="If true (default), run nodes in an external process. Set to 'false' to run inline.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Timeout in minutes after which the runner will terminate",
    )

    args = parser.parse_args()
    # Run the async main function
    asyncio.run(async_main(args))
    pid = os.getpid()
    logger.info(f"runner with pid {pid} shutting down normally")


if __name__ == "__main__":
    runner_main()
