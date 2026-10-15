import asyncio
import logging

from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.core.services.base_service import RestartService

logger = logging.getLogger("NodeRunner")


class TimeoutRestartService(RestartService):
    """Service that restarts the runner after a specified timeout"""

    def __init__(self, resource: Resource, timeout_minutes: int, shutdown_event=None):
        if isinstance(timeout_minutes, bool) or timeout_minutes <= 0:
            raise ValueError("Runner restart timeout must be positive")
        super().__init__(
            "TimeoutRestart",
            resource,
            interval=timeout_minutes * 60,
            shutdown_event=shutdown_event,
        )
        self._timeout_minutes = timeout_minutes
        self._executed = False

    async def _run_loop(self):
        self._executed = False
        # Other periodic services run immediately; this one is a deadline, not
        # a poller. Stopping the runner must also interrupt the initial wait.
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
        except asyncio.TimeoutError:
            await super()._run_loop()

    async def execute(self):
        # Only execute once after the timeout interval
        if not self._executed:
            self._executed = True
            logger.info(
                f"Timeout of {self._timeout_minutes} minutes reached. Triggering restart..."
            )
            try:
                await self.write_resource_event(
                    RunnerEventEnum.SHUTDOWN,
                    message=f"Timeout restart after {self._timeout_minutes} minutes",
                )
            finally:
                # A telemetry outage must not prevent the configured shutdown.
                await self.trigger_restart()
