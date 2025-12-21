import argparse
import asyncio
import logging
import os
import socket
import subprocess
from datetime import datetime
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

class BaseService:
    """A managed periodic service that can be stopped gracefully"""

    def __init__(self, name: str, interval: int):
        self.name = name
        self.interval: int = interval
        self._stop_event = asyncio.Event()
        self._task = None

    async def _run_loop(self):
        """Internal loop that respects the stop event"""
        logger.info(f"Service {self.name} started.")
        while not self._stop_event.is_set():
            start_time = asyncio.get_event_loop().time()
            try:
                await self.execute()
            except Exception as e:
                logger.exception(f"Error in service {self.name}: {e}")

            # Calculate wait time to maintain interval regardless of execution duration
            elapsed = asyncio.get_event_loop().time() - start_time
            wait_time = max(0, int(self.interval - elapsed))

            try:
                # Wait for interval OR until stop event is set
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_time)
            except asyncio.TimeoutError:
                # Normal path: interval passed, loop again
                pass

        logger.info(f"Service {self.name} stopped.")

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


class GitUvUpdateService(BaseService):
    """
    Advanced service that performs git pull and uv lock upgrades.
    If changes are detected, it restarts the runner.
    """

    def __init__(self, manager, resource_name,  interval):
        super().__init__("GitUvUpdate", interval)
        self.manager = manager
        # Resolve project root (assuming we are in src/simstack/core/runner.py)
        self.project_dir = context.config.project_root.resolve(strict=True)
        self.uv_lock_path = context.config.project_root / "uv.lock"
        self.pid_file = context.config.workdir / "runner.pid"
        self.counter = 2

    async def _run_command(self, cmd: list) -> str:
        """Run a shell command and return output"""
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.warning(f"Command {' '.join(cmd)} failed: {stderr.decode()}")
        return stdout.decode().strip()

    async def execute(self):
        logger.info("Checking for updates (Git + UV)...")

        # 1. Git Pull and check for changes
        # 'git pull' output contains 'Already up to date.' if nothing changed
        git_output = await self._run_command(["git", "pull"])
        git_changed = "Already up to date." not in git_output

        # 2. UV Lock upgrade with checksums
        old_checksum = get_file_checksum(self.uv_lock_path)
        await self._run_command(["uv", "lock", "--upgrade"])
        new_checksum = get_file_checksum(self.uv_lock_path)
        uv_changed = old_checksum != new_checksum
        self.counter += 1

        if git_changed or uv_changed or self.counter >= 2:
            reason = "Git changes" if git_changed else "UV lock changes"
            logger.info(f"Update detected ({reason}). Triggering restart...")
            await self.trigger_restart()

    async def trigger_restart(self):
        """Spawns an independent process to kill current PID and start new one"""
        current_pid = os.getpid()

        # Write current PID to file so the restarter knows who to kill
        self.pid_file.write_text(str(current_pid))

        # Command to restart depends on OS
        # We use sys.executable to ensure we use the same Python interpreter
        # We'll call the runner module again
        script_path = Path(__file__).resolve()
        args = [sys.executable, str(script_path)] + sys.argv[1:]

        # We need a small helper script or a one-liner that:
        # 1. Waits for this process to die (to avoid port/resource conflicts)
        # 2. Starts the new one
        if platform.system() == "Windows":
            # Windows 'start' command handles detachment well
            # Use ping for a 2-second delay as 'timeout' fails in non-interactive shells
            cmd = f"taskkill /F /PID {current_pid} && ping 127.0.0.1 -n 3 > nul && {' '.join(args)}"
            subprocess.Popen(cmd, shell=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            # Linux: use a subshell that nohup/disowns
            # Kill, wait, and start
            cmd = f"kill -9 {current_pid} && sleep 2 && {' '.join(args)}"
            subprocess.Popen(["/bin/bash", "-c", cmd], start_new_session=True)

        # The above commands kill us, so this line might not even log
        logger.info("Restart signal sent. Goodbye!")


class JobPollingService(BaseService):
    def __init__(self, manager, resource_name, interval, max_concurrent):
        super().__init__("JobPolling", interval)
        self.manager = manager
        self.resource_name = resource_name
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.running_tasks = set()

    async def execute(self):
        # Clean up the completed tasks
        completed_tasks = {task for task in self.running_tasks if task.done()}
        for task in completed_tasks:
            try:
                await task
            except Exception as e:
                logger.exception(f"Task completed with error: {e}")
            self.running_tasks.remove(task)

        # Check for STOP file
        for path in [context.config.workdir, context.config.project_root]:
            stop_file = Path(path) / "STOP"
            if stop_file.exists():
                await self.manager.write_resource_event(RunnerEventEnum.SHUTDOWN, message="STOP file found")
                logger.info(f"STOP file found at {stop_file}, signaling shutdown...")
                await self.manager.stop_all_services()
                return

        # Load tasks
        registry_entry_list = await context.db.load_waiting_tasks_for_resource(self.resource_name)
        if registry_entry_list:
            logger.info(f"Retrieved {len(registry_entry_list)} tasks for {self.resource_name}")
            for entry in registry_entry_list:
                task = asyncio.create_task(self._run_with_semaphore(entry))
                self.running_tasks.add(task)

    async def _run_with_semaphore(self, entry):
        async with self.semaphore:
            return await self.manager.run_node(entry)


class RunnerStatusService(BaseService):
    def __init__(self, manager, interval):
        super().__init__("RunnerStatus", interval)
        self.manager = manager

    async def execute(self):
        await self.manager.write_resource_event(RunnerEventEnum.ALIVE)


class SlurmStatusService(BaseService):
    def __init__(self, manager, resource_name, interval):
        super().__init__("SlurmStatus", interval)
        self.manager = manager
        self.resource_name = resource_name

    async def execute(self):
        running_jobs = await context.db.engine.find(
            NodeRegistry,
            (NodeRegistry.status == TaskStatus.RUNNING)
            & (NodeRegistry.parameters.resource == self.manager.resource),
        )
        for job in running_jobs:
            if job.job_id is not None:
                slurm_info = get_job_info(job.job_id, job.id, Resource(value=self.resource_name))
                slurm_entry = await context.db.find_one(SlurmInfo, SlurmInfo.job_id == job.job_id)

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
                    check_job = await context.db.engine.find_one(NodeRegistry, NodeRegistry.id == job.id)
                    if slurm_entry:
                        await context.db.delete(slurm_entry)
                    if check_job.status == TaskStatus.RUNNING:
                        job.job_id = None
                        job.status = TaskStatus.TIME_OUT
                        await context.db.save(job)

        await clean_slurm_info(self.manager.username, self.manager.resource)


class GitRestartService(BaseService):
    """Service that runs an external script to check git/restart the runner"""

    def __init__(self, manager, resource_name, interval):
        super().__init__("GitRestart", interval)
        self.manager = manager
        self.resource_name = resource_name



class RunnerManager:
    def __init__(self, resource: Resource):
        self.resource = resource
        self.pid = os.getpid()
        self.username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        self.hostname = socket.gethostname()
        self.time_started = datetime.now()
        self.services = []

    async def stop_all_services(self):
        """Gracefully stop all registered services"""
        logger.info("Stopping all services...")
        await asyncio.gather(*(s.stop() for s in self.services), return_exceptions=True)

    def _get_uptime_string(self) -> str:
        time_diff = datetime.now() - self.time_started
        days = time_diff.days
        hours, remainder = divmod(time_diff.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m {seconds}s"

    async def write_node_event(self, event: RunnerEventEnum, node_id: ObjectId, message: str = None):
        runner_event = RunnerEvent(
            runner_type=RunnerType.NODE_RUNNER,
            event=event,
            pid=self.pid,
            hostname=self.hostname,
            user=self.username,
            resource=self.resource,
            node_id=node_id,
            message=message,
        )
        await context.db.save(runner_event)

    async def write_resource_event(self, event: RunnerEventEnum, message: str = None):

        git_list = make_git_status_list()
        if event == RunnerEventEnum.ALIVE:

            time_diff = datetime.now() - self.time_started
            days = time_diff.days
            hours, remainder = divmod(time_diff.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime = f"{days}d {hours}h {minutes}m {seconds}s"
            message = f"Uptime: {uptime}"

            runner_event = await context.db.find_one(
                RunnerEvent,
                (RunnerEvent.runner_type == RunnerType.RESOURCE_RUNNER)
                & (RunnerEvent.resource == self.resource)
                & (RunnerEvent.event == RunnerEventEnum.ALIVE)
                & (RunnerEvent.pid == self.pid),
            )
            if runner_event:
                runner_event.message = message
                runner_event.timestamp = datetime.now()
                runner_event.git_status = git_list
                await context.db.save(runner_event)
                return  # updated existing event

        runner_event = RunnerEvent(
            runner_type=RunnerType.RESOURCE_RUNNER,
            pid=self.pid,
            hostname=self.hostname,
            user=self.username,
            timestamp=datetime.now(),
            git_status=git_list,
            event=event,
            resource=self.resource,
            message=message,
        )
        await context.db.save(runner_event)

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

    async def run_nodes_for_resource(self, polling_interval=5, max_concurrent=10):
        """Orchestrates multiple independent services"""
        # Save PID on startup
        project_dir =context.config.workdir
        (project_dir / "runner.pid").write_text(str(os.getpid()))
        
        await self.write_resource_event(RunnerEventEnum.RUNNER_STARTED)

        self.services = [
            JobPollingService(self, str(self.resource), polling_interval, max_concurrent),
            RunnerStatusService(self, interval=60),
            SlurmStatusService(self, str(self.resource), interval=30),
            GitUvUpdateService(self, str(self.resource), interval=60), # Advanced check every hour
        ]

        # Start all and wait for them
        tasks = [service.start() for service in self.services]
        
        try:
            # We use gather to keep the manager alive while services run
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await self.stop_all_services()
            raise


async def async_main(args):
    """Async entry point"""
    await context.initialize(resource=args.resource, db_name=args.db_name)
    if args.resource:
        logger.info(f"Setting resource for runner to {args.resource}")
        runner_manager = RunnerManager(context.config.resource)
        await runner_manager.run_nodes_for_resource(args.polling_interval, 10)

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

    args = parser.parse_args()

    # Run the async main function
    asyncio.run(async_main(args))
    pid = os.getpid()
    logger.info(f"runner with pid {pid} shutting down normally")


if __name__ == "__main__":
    runner_main()
