from simstack.models.parameters import Resource
from simstack.core.services.base_service import BaseService

class GitRestartService(BaseService):
    """Service that runs an external script to check git/restart the runner"""

    def __init__(self, resource: Resource, interval):
        super().__init__("GitRestart", resource, interval)
        self._resource_name = str(resource)

    def execute(self):
        raise NotImplementedError("GitRestartService must implement execute()")
