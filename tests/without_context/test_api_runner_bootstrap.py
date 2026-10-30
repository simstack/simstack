from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.context import GlobalState
from simstack.core.definitions import DBType
from simstack.models.resource_definition import ResourceDefinition
from simstack.runner_api.persistence import HTTPMotorClient
from simstack.runner_api.transport import RunnerAPIClient, RunnerAPIError


@pytest.mark.asyncio
async def test_api_bootstrap_opens_no_mongo_connection_and_erases_db_env(
    tmp_path, monkeypatch
):
    import simstack.util.db as db_module
    import simstack.runner_api.logging as api_logging
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

    async def json(self, method, path, body=None):
        calls.append((method, path))
        if path == "/runner/bootstrap":
            return {
                "resource": "cluster",
                "resource_definition": resource.model_dump(mode="json"),
                "resources": ["cluster"],
            }
        return {"result": "[]"}

    def forbidden(*args, **kwargs):
        raise AssertionError("A runner opened a MongoDB client")

    monkeypatch.setattr(RunnerAPIClient, "json", json)
    monkeypatch.setattr(db_module, "AsyncIOMotorClient", forbidden)
    monkeypatch.setattr(api_logging, "setup_runner_logging", lambda *args: None)
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", "")
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://example.test/api")
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "resource-token")
    monkeypatch.setenv("SIMSTACK_DB_CONNECTION_STRING", "mongodb://must-not-be-used")
    monkeypatch.setenv("MONGODB_URI", "mongodb://must-not-be-used")
    await state.initialize(project_root=tmp_path, api_only=True, resource="cluster")
    assert state.db.databae_type == DBType.API
    assert isinstance(state.db.client, HTTPMotorClient)
    assert state.config.connection_string is None
    assert state.config.db_name is None
    assert state.config.workdir == tmp_path / "configured-shared-work"
    assert state.config.workdir.is_dir()
    import os

    assert "SIMSTACK_DB_CONNECTION_STRING" not in os.environ
    assert "MONGODB_URI" not in os.environ
    assert calls[0] == ("GET", "/runner/bootstrap")


@pytest.mark.asyncio
async def test_runner_rejects_explicit_database_credentials(tmp_path, monkeypatch):
    from simstack.core import runner

    context = SimpleNamespace(initialize=AsyncMock())
    monkeypatch.setattr(runner, "context", context)
    args = SimpleNamespace(db_name="private-db", connection_string="mongodb://secret")
    with pytest.raises(ValueError, match="no longer supported"):
        await runner.async_main(args)
    context.initialize.assert_not_called()


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
