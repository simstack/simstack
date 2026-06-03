import logging
from datetime import datetime, timedelta

from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerType
from simstack.core.services.base_service import BaseService

logger = logging.getLogger("NodeRunner")

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
        old_events = await context.db.find(
            RunnerEvent,
            (RunnerEvent.runner_type == RunnerType.RESOURCE_RUNNER)
            & (RunnerEvent.resource == self._resource)
            & (RunnerEvent.timestamp < cutoff_time)
        )

        if old_events:
            logger.info(f"Cleaning up {len(old_events)} old RunnerEvent logs for resource {self._resource}")
            for event in old_events:
                await context.db.delete(event)
