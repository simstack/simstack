from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.core.services.base_service import BaseService


class RunnerStatusService(BaseService):
    def __init__(self, resource: Resource, interval: int) -> None:
        super().__init__("RunnerStatus", resource, interval)

    async def execute(self) -> None:
        await self.write_resource_event(RunnerEventEnum.ALIVE)
