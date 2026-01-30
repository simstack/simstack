import asyncio
import logging
from pathlib import Path

from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.core.services.base_service import BaseService

logger = logging.getLogger("NodeRunner")

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
