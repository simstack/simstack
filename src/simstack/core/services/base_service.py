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
from simstack.models.document import ObjectId

from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerType, RunnerEventEnum
from simstack.util.git_repository_status import make_git_status_list

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
        await context.server.resource.event(runner_event)

    async def write_resource_event(self, event: RunnerEventEnum, message: str = None):
        git_list = make_git_status_list(context.config.project_root)
        if event == RunnerEventEnum.ALIVE:
            uptime = self._get_uptime_string()
            message = f"Uptime: {uptime}"


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
        await context.server.resource.event(runner_event)

    async def _run_loop(self):
        """Internal loop that respects the stop event"""
        logger.info(f"Service {self._name} started.")
        while not self._stop_event.is_set():
            start_time = asyncio.get_event_loop().time()
            try:
                await self.execute()
                self._consecutive_failures = 0
            except Exception as e:
                from simstack.server_api.http_transport import RunnerAPIError
                if isinstance(e, RunnerAPIError):
                    # The persistent control agent survives server/network outages.
                    self._consecutive_failures = 0
                    logger.warning("Service %s is waiting for the server API", self._name)
                else:
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
        os.execv(sys.executable, [sys.executable, "-m", "simstack.core.runner", *sys.argv[1:]])
