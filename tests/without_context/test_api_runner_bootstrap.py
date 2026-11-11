from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.context import GlobalState
from simstack.models.resource_definition import ResourceDefinition
from simstack.server_api.http_transport import RunnerAPIClient, RunnerAPIError


@pytest.mark.asyncio
async def test_api_bootstrap_opens_no_mongo_connection_and_erases_db_env(
    tmp_path, monkeypatch
):
    import simstack.server_api.log_upload as api_logging
    from simstack.core.resources import allowed_resources

    monkeypatch.setattr(allowed_resources, "_initialized", False)
    monkeypatch.setattr(allowed_resources, "_resources", [])
    monkeypatch.setattr(GlobalState, "_instance", None)
    state = GlobalState()
    resource = ResourceDefinition(
        resource_str="cluster",
        hostname="machine",
        workdir="$PROJECT/configured-shared-work",
        routes=[],
    )
    calls = []

    async def json(self, method, path, body=None, **kwargs):
        calls.append((method, path))
        if path == "/runner/v3/bootstrap":
            return {
                "account_id": "test-account",
                "protocol_version": 3,
                "resource": "cluster",
                "resource_definition": resource.model_dump(mode="json"),
                "resources": ["cluster"],
                "repository_reconciliation_required": True,
            }
        if path == "/runner/repositories/reconcile":
            assert body == {"prepared": []}
            return {"reconciled": True}
        return {"graph": {"roots": [], "nodes": []}}

    def forbidden(*args, **kwargs):
        raise AssertionError("A runner opened a MongoDB client")

    monkeypatch.setattr(RunnerAPIClient, "json", json)
    monkeypatch.setattr(api_logging, "setup_runner_logging", lambda *args: None)
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", "")
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://example.test/api")
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "resource-token")
    monkeypatch.setenv("SIMSTACK_DB_CONNECTION_STRING", "mongodb://must-not-be-used")
    monkeypatch.setenv("MONGODB_URI", "mongodb://must-not-be-used")
    monkeypatch.setattr("simstack.util.repository_runtime.prepared_repositories", lambda: [])
    await state.initialize(project_root=tmp_path, resource="cluster")
    assert not hasattr(state, "db")
    assert not hasattr(state, "store")
    assert not hasattr(state.config, "connection_string")
    assert not hasattr(state.config, "db_name")
    assert state.config.workdir == tmp_path / "configured-shared-work"
    assert state.config.workdir.is_dir()
    import os

    assert "SIMSTACK_DB_CONNECTION_STRING" not in os.environ
    assert "MONGODB_URI" not in os.environ
    assert calls[0] == ("GET", "/runner/v3/bootstrap")
    assert calls[1] == ("POST", "/runner/repositories/reconcile")


@pytest.mark.asyncio
async def test_runner_rejects_explicit_database_credentials(tmp_path, monkeypatch):
    from simstack.core import runner

    state = GlobalState()
    with pytest.raises(ValueError, match="database configuration"):
        await state.initialize(db_name="private-db", connection_string="mongodb://secret")


@pytest.mark.asyncio
async def test_control_agent_keeps_retrying_api_outages(monkeypatch):
    from simstack.core.services.base_service import BaseService

    class Reconnecting(BaseService):
        def __init__(self):
            super().__init__("reconnect", "test", 0)
            self.attempts = 0

        async def execute(self):
            self.attempts += 1
            if self.attempts == 5:
                self._stop_event.set()
                return
            raise RunnerAPIError("offline")

    service = Reconnecting()
    await service.start()
    assert service.attempts == 5
    assert service._consecutive_failures == 0


@pytest.mark.asyncio
async def test_initial_control_failure_stays_paused_and_retries(tmp_path, monkeypatch):
    import asyncio
    from simstack.core.services import runner_manager, runner_control_service
    from simstack.core.services.base_service import BaseService

    context = SimpleNamespace(config=SimpleNamespace(workdir=tmp_path))
    monkeypatch.setattr(runner_manager, "context", context)

    class Idle(BaseService):
        def __init__(self, resource, *args, **kwargs):
            super().__init__("idle", resource, 0)
        async def execute(self):
            pass

    for name in ("RunnerStatusService", "RunnerCleanupService", "StopCheckService", "FileTransferService"):
        monkeypatch.setattr(runner_manager, name, Idle)
    manager = runner_manager.RunnerManager("test", with_jobs=False, no_pull=True)
    calls = []
    async def json(method, path, body=None):
        assert manager._claiming_paused.is_set()
        calls.append(path)
        if len(calls) == 1:
            raise RunnerAPIError("temporary first-request outage")
        manager._services[0]._stop_event.set()
        asyncio.get_running_loop().call_soon(manager._shutdown_event.set)
        return {"paused": False, "commands": []}
    from simstack.server_api import ServerAPI
    monkeypatch.setattr(runner_control_service, "context", SimpleNamespace(server=ServerAPI(SimpleNamespace(json=json))))
    original = runner_control_service.RunnerControlService
    def control(active):
        service = original(active)
        service._interval = 0
        return service
    monkeypatch.setattr(runner_control_service, "RunnerControlService", control)
    await asyncio.wait_for(manager.run_nodes_for_resource(), 3)
    assert calls == ["/runner/control", "/runner/control"]
    assert not manager._claiming_paused.is_set()


def test_explicit_machine_config_survives_pinned_project_selection(tmp_path, monkeypatch):
    from simstack.runner_runtime.execution_environment import task_resource_config, task_environment
    from simstack.util.resource_config import ResourceConfig
    machine = tmp_path/"machine.toml"
    machine.write_text('[test.programs.fixture]\ndocker_image="machine-image"\n')
    source = tmp_path/"pinned-source"
    source.mkdir()
    (source/"config.toml").write_text('[test.programs.fixture]\ndocker_image="repository-image"\n')
    monkeypatch.setenv("SIMSTACK_MACHINE_CONFIG", str(machine))
    entry = SimpleNamespace(id="0"*24, repository_revision="a"*64, _repository_project_root=source)
    context = SimpleNamespace(config=SimpleNamespace(project_root=tmp_path, resource="test"), resource_config=ResourceConfig(machine,"test"))
    assert task_resource_config(entry, context)._config_path == machine
    api = SimpleNamespace(requests=SimpleNamespace(server_url="https://example.test/api", authorization="runner",
        resource="test", bootstrap_data={"account_id": "test-account"}))
    assert task_environment(entry, server=api)["SIMSTACK_MACHINE_CONFIG"] == str(machine)
