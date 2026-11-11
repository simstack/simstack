"""Runner bootstrap, resource metadata, events and local cache observations."""

from simstack.models.document.codec import decode_document_value, encode_document_value
from simstack.server_api.server_requests import PROTOCOL_VERSION
from simstack.server_api.http_transport import RunnerAPIError
from simstack.server_api.workflow_tasks import decode_task_response


class ResourceControlAPI:
    def __init__(self, requests):
        self.requests = requests

    async def bootstrap(self):
        result = await self.requests.read("/bootstrap")
        if result.get("protocol_version") != PROTOCOL_VERSION:
            raise RunnerAPIError(
                "Server and Runner protocol versions differ; update the coordinated release"
            )
        if not isinstance(result.get("account_id"), str) or not result["account_id"]:
            raise RunnerAPIError("Server bootstrap has no account identity")
        self.requests.bootstrap_data = result
        self.requests.transport.account_id = result["account_id"]
        definition = decode_document_value(result["resource_definition"])
        self.requests.resource = definition["resource_str"]
        return result

    async def event(self, event):
        return await self.requests.command(
            "/events", {"event": encode_document_value(event)}
        )

    async def resource_definition(self):
        from simstack.models.resource_definition import ResourceDefinition

        result = await self.requests.read("/resource")
        return ResourceDefinition.model_validate(
            decode_document_value(result["resource_definition"])
        )

    async def active_containers(self):
        result = await self.requests.read("/work/containers")
        return [decode_task_response(item) for item in result.get("tasks", [])]

    async def cache_locations(self):
        return await self.requests.read("/cache/locations")

    async def cache_deleted(self, file_id, path, *, observed_instance_id=None):
        return await self.requests.command(
            "/cache/deleted", {"file_id": str(file_id), "path": str(path),
                "observed_instance_id": observed_instance_id}
        )

    async def control_state(self):
        return await self.requests.transport.json("GET", "/runner/control")

    async def control_status(self, command_id, status):
        return await self.requests.transport.json(
            "POST", f"/runner/control/{command_id}/status", {"status": status}
        )

    async def acknowledge_control(self, command_id, result):
        return await self.requests.transport.json(
            "POST", f"/runner/control/{command_id}/ack", result
        )

    async def repository_revision(self, revision):
        if self.requests.authorization in {"user", "local"}:
            return await self.requests.read(f"/repositories/{revision}")
        return await self.requests.transport.json(
            "GET", f"/runner/repositories/{revision}"
        )

    async def reconcile_repositories(self, inventory):
        return await self.requests.transport.json(
            "POST", "/runner/repositories/reconcile", {"prepared": inventory}
        )
