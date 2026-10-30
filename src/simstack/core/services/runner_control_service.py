from simstack.core.services.base_service import BaseService
from simstack.runner_api.transport import RunnerAPIClient


class RunnerControlService(BaseService):
    """Keep the control channel alive while job claiming is paused."""

    def __init__(self, manager):
        super().__init__("RunnerControl", manager._resource, 5)
        self.manager = manager
        self.api = RunnerAPIClient.from_environment()
        self._container_credentials = {}

    async def execute(self):
        state = await self.api.json("GET", "/runner/control")
        await self.refresh_container_credentials()
        if state.get("paused"):
            self.manager._claiming_paused.set()
        else:
            self.manager._claiming_paused.clear()
        for command in state.get("commands", []):
            if command["action"] == "restart":
                await self.api.json("POST", f"/runner/control/{command['id']}/ack", {})
                self.manager._restart_requested = True
                self.manager._shutdown_event.set()
                return


    async def refresh_container_credentials(self):
        import asyncio
        import hashlib
        import json
        import os
        from pathlib import Path

        from simstack.core.context import context
        from simstack.core.run_docker import docker_cidfile_path, read_container_id
        from simstack.models.node_registry import NodeRegistry
        from simstack.runner_api.credentials import (
            copy_container_credentials,
            read_credentials,
        )

        path = os.environ.get("SIMSTACK_RUNNER_CREDENTIALS")
        if not path:
            return
        credentials = read_credentials(path)
        digest = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).digest()
        tasks = await context.db.find(NodeRegistry, {
            "parameters.resource.value": str(self.manager._resource),
            "parameters.in_docker": True,
            "status": {"$in": ["retrieved", "running", "slurm_queued", "slurm_running"]},
        })
        active = set()
        for task in tasks:
            cidfile = docker_cidfile_path(Path(context.config.workdir) / task.name / str(task.id))
            container_id = read_container_id(cidfile)
            if not container_id:
                continue
            active.add(container_id)
            if self._container_credentials.get(container_id) == digest:
                continue
            try:
                copied = await asyncio.to_thread(copy_container_credentials, container_id, credentials)
            except (OSError, TimeoutError):
                copied = False
            if copied:
                self._container_credentials[container_id] = digest
        self._container_credentials = {key: value for key, value in self._container_credentials.items() if key in active}
