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
        import os
        from simstack.runner_api.credentials import configure_git_credentials, read_credentials, update_known_hosts
        credentials = os.environ.get("SIMSTACK_RUNNER_CREDENTIALS")
        if credentials:
            configure_git_credentials(read_credentials(credentials))
        update_known_hosts(state.get("known_hosts"))
        await self.refresh_container_credentials()
        if state.get("paused"):
            self.manager._claiming_paused.set()
        else:
            self.manager._claiming_paused.clear()
        for command in state.get("commands", []):
            if command["action"] == "prepare_repository":
                if state.get("busy") or command.get("status") == "deferred_busy":
                    continue
                key_version = state.get("ssh_key_version")
                key_status = state.get("ssh_key_status")
                requires_reinstall = (key_status is not None and key_status != "active") or (
                    key_version is not None and str(key_version) != os.environ.get("SIMSTACK_GIT_SSH_KEY_VERSION"))
                await self.prepare_repository(command, requires_reinstall=requires_reinstall)
                continue
            if command["action"] == "restart":
                await self.api.json("POST", f"/runner/control/{command['id']}/ack", {})
                self.manager._restart_requested = True
                self.manager._shutdown_event.set()
                return

    async def prepare_repository(self, command, *, requires_reinstall=False):
        from simstack.util.repository_runtime import prepare_repository

        path = f"/runner/control/{command['id']}"
        # The Server checks persisted task states and reserves preparation
        # atomically with claim admission. Never infer this from local processes.
        state = await self.api.json("POST", path + "/status", {"status": "preparing"})
        if state.get("status") != "preparing":
            return
        if requires_reinstall:
            await self.api.json("POST", path + "/ack", {
                "status": "failed", "error": "Git SSH key was revoked or rotated; rerun the Runner installer",
            })
            return
        try:
            await prepare_repository(self.api, command["payload"])
        except Exception:
            await self.api.json("POST", path + "/ack", {
                "status": "failed",
                "error": "Repository preparation failed; check Git host trust, access, pinned revisions, uv.lock and extras",
            })
        else:
            await self.api.json("POST", path + "/ack", {"status": "ready"})


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
        from simstack.persistence.criteria import eq
        tasks = await context.db.find(NodeRegistry,
            eq("parameters.resource.value", str(self.manager._resource)),
            eq("parameters.in_docker", True),
            NodeRegistry.status.in_(["retrieved", "running", "slurm_queued", "slurm_running"]),
        )
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
