"""Explicit Server APIs for workflow tasks, scientific values and Runner resources."""

from simstack.server_api.http_transport import RunnerAPIClient
from simstack.server_api.server_requests import ServerRequests
from simstack.server_api.scientific_values import ScientificValuesAPI
from simstack.server_api.workflow_tasks import WorkflowTasksAPI
from simstack.server_api.slurm_jobs import SlurmJobsAPI
from simstack.server_api.resource_control import ResourceControlAPI


class ServerAPI:
    def __init__(self, transport, *, authorization="runner", resource="self"):
        self.requests = ServerRequests(
            transport, authorization=authorization, resource=resource
        )
        self.values = ScientificValuesAPI(self.requests)
        self.tasks = WorkflowTasksAPI(self.requests, self.values)
        self.slurm = SlurmJobsAPI(self.requests, self.tasks)
        self.resource = ResourceControlAPI(self.requests)

    @classmethod
    def for_user(cls, server_url, user_token, *, resource="self"):
        return cls(
            RunnerAPIClient(server_url, user_token, rotate_credentials=False),
            authorization="user",
            resource=resource,
        )

    @classmethod
    def for_local_execution(cls, server_url, execution_token, *, resource="self"):
        return cls(
            RunnerAPIClient(server_url, execution_token, rotate_credentials=False),
            authorization="local",
            resource=resource,
        )
