import logging

from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.core.services.base_service import RestartService

logger = logging.getLogger("NodeRunner")

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
