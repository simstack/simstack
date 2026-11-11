from simstack.core.services.base_service import BaseService
from simstack.core.context import context


class RunnerControlService(BaseService):
    """Keep the control channel alive while job claiming is paused."""

    def __init__(self, manager):
        super().__init__("RunnerControl", manager._resource, 5)
        self.manager = manager
        self.control = context.server.resource
        self._container_credentials = {}
        self._pending_restart = None

    async def execute(self):
        state = await self.control.control_state()
        import os
        from simstack.runner_runtime.runner_credentials import configure_git_credentials, read_credentials, update_known_hosts
        credentials = os.environ.get("SIMSTACK_RUNNER_CREDENTIALS")
        if credentials:
            configure_git_credentials(read_credentials(credentials))
        update_known_hosts(state.get("known_hosts"))
        await self.refresh_container_credentials()
        if state.get("paused"):
            self.manager._claiming_paused.set()
        else:
            self.manager._claiming_paused.clear()
        commands = list(state.get("commands", []))
        if self._pending_restart is not None and not any(
                item["id"] == self._pending_restart["id"] for item in commands):
            commands.insert(0, self._pending_restart)
        for command in commands:
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
                # ACK is idempotent. Retain the received intent if its committed
                # response is lost and the next poll no longer returns it.
                self._pending_restart = command
                await self.control.acknowledge_control(command["id"], {})
                self.manager._restart_requested = True
                self.manager._shutdown_event.set()
                return

    async def prepare_repository(self, command, *, requires_reinstall=False):
        from simstack.util.repository_runtime import prepare_repository

        # The Server checks persisted task states and reserves preparation
        # atomically with claim admission. Never infer this from local processes.
        state = await self.control.control_status(command["id"], "preparing")
        if state.get("status") != "preparing":
            return
        if requires_reinstall:
            await self.control.acknowledge_control(command["id"], {
                "status": "failed", "error": "Git SSH key was revoked or rotated; rerun the Runner installer",
            })
            return
        try:
            await prepare_repository(context.server.requests, command["payload"], repair_runtime=True)
        except Exception:
            await self.control.acknowledge_control(command["id"], {
                "status": "failed",
                "error": "Repository preparation failed; check Git host trust, access, pinned revisions, uv.lock and extras",
            })
        else:
            await self.control.acknowledge_control(command["id"], {"status": "ready"})


    async def refresh_container_credentials(self):
        import asyncio
        import hashlib
        import json
        import os
        from pathlib import Path

        from simstack.core.context import context
        from simstack.core.run_docker import docker_cidfile_path, read_container_id
        from simstack.runner_runtime.runner_credentials import (
            copy_container_credentials,
            read_credentials,
        )

        path = os.environ.get("SIMSTACK_RUNNER_CREDENTIALS")
        if not path:
            return
        credentials = read_credentials(path)
        digest = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).digest()
        tasks = await context.server.resource.active_containers()
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
