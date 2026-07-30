import asyncio
import logging
import os
import socket
import sys
import platform
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from odmantic import ObjectId

from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerType, RunnerEventEnum
from simstack.util.runner_utils import make_git_status_list

logger = logging.getLogger("NodeRunner")

class BaseService(ABC):
    """A managed periodic service that can be stopped gracefully"""

    def __init__(self, name: str, resource: Resource, interval: int, shutdown_event: asyncio.Event = None):
        self._name = name
        self._resource = resource
        self._interval: int = interval
        self._stop_event = asyncio.Event()
        self._shutdown_event = shutdown_event
        self._task = None
        self._consecutive_failures = 0
        self._log_traceback_on_failure = True

        # Common identity attributes
        self._pid = os.getpid()
        self._hostname = socket.gethostname()
        self._username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        self._time_started = datetime.now()

    def _get_uptime_string(self) -> str:
        time_diff = datetime.now() - self._time_started
        total_seconds = int(time_diff.total_seconds())
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
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
                self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                msg = f"Error in service {self._name} (failure {self._consecutive_failures}/3): {e}"
                if self._log_traceback_on_failure:
                    logger.exception(msg)
                else:
                    logger.error(msg)

                if self._consecutive_failures >= 3:
                    logger.error(f"Service {self._name} failed 3 times in a row. Shutting down.")
                    self._stop_event.set()
                    if self._shutdown_event:
                        self._shutdown_event.set()

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
        # IMPORTANT: We need the path to the original runner.py script.
        # Since we are now in services/base_service.py, we need to go up two levels.
        # But actually Path(__file__).resolve() in the original runner.py was used.
        # We should probably pass the runner script path or rely on how it was invoked.
        # In runner.py it was: script_path = Path(__file__).resolve()
        
        # Let's try to find runner.py relative to this file.
        script_path = (Path(__file__).parent.parent / "runner.py").resolve()

        log_file = context.config.workdir / "runner.out"

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
