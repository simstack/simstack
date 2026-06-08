from simstack.models.parameters import Resource
from simstack.core.services.base_service import BaseService


class GitRestartService(BaseService):
    """Service that runs an external script to check git/restart the runner"""

    def __init__(self, resource: Resource, interval: int) -> None:
        super().__init__("GitRestart", resource, interval)
        self._resource_name = str(resource)

    async def execute(self) -> None:
        raise NotImplementedError("GitRestartService must implement execute()")
