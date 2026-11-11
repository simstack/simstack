from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from simstack.core.context import GlobalState
from simstack.core.resources import allowed_resources
from simstack.models.resource_definition import ResourceDefinition

@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(GlobalState, "_instance", None)
    allowed_resources.clear_resources()
    monkeypatch.setattr("simstack.server_api.log_upload.setup_runner_logging", lambda *args: None)
    value = GlobalState()
    yield value
    allowed_resources.clear_resources()

def client(tmp_path):
    resource = ResourceDefinition(resource_str="test", hostname="localhost", workdir=str(tmp_path / "work"))
    return SimpleNamespace(
        resource=SimpleNamespace(bootstrap=AsyncMock(return_value={"account_id": "test-account", "resource": "test", "resource_definition": resource.model_dump(mode="json"), "resources": ["test"]})),
        values=SimpleNamespace(catalogue_models=AsyncMock(return_value=[]), catalogue_nodes=AsyncMock(return_value=[])),
        requests=SimpleNamespace(authorization="runner", server_url="https://example.test/api", token="test-token", transport=SimpleNamespace()),
    )

def test_singleton_and_uninitialized_access(state):
    assert GlobalState() is state
    assert not state.initialized
    with pytest.raises(RuntimeError, match="API credentials"):
        _ = state.server
    with pytest.raises(RuntimeError, match="Initialize"):
        _ = state.config
    assert not hasattr(state, "db")
    assert not hasattr(state, "_db")

@pytest.mark.asyncio
async def test_bootstrap_preserves_local_execution_configuration(state, tmp_path):
    api = client(tmp_path)
    await state.initialize(server=api, project_root=tmp_path, resource="test", in_docker=True)
    assert state.initialized
    assert state.server is api
    assert state.config.workdir == tmp_path / "work"
    assert state.in_docker
    assert state.current_node_name is None
    state.current_node_name = "optimizer"
    assert state.current_node_name == "optimizer"
    with pytest.raises(AttributeError):
        state.server = None

@pytest.mark.asyncio
async def test_repeated_initialize_does_not_reset_live_client(state, tmp_path):
    api = client(tmp_path)
    await state.initialize(server=api, project_root=tmp_path)
    await state.initialize(project_root=tmp_path)
    api.resource.bootstrap.assert_awaited_once()

@pytest.mark.asyncio
async def test_failed_bootstrap_is_not_initialized(state, tmp_path):
    api = client(tmp_path)
    api.resource.bootstrap.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        await state.initialize(server=api, project_root=tmp_path)
    assert not state.initialized

@pytest.mark.asyncio
async def test_resource_mismatch_rejected_before_catalogue_import(state, tmp_path):
    api = client(tmp_path)
    with pytest.raises(ValueError, match="different resource"):
        await state.initialize(server=api, project_root=tmp_path, resource="other")
    api.values.catalogue_models.assert_not_awaited()


@pytest.mark.asyncio
async def test_launcher_account_must_match_bootstrap_before_loading_code(state, monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_EXECUTION_ACCOUNT_ID", "different-account")
    api = client(tmp_path)
    with pytest.raises(ValueError, match="different account"):
        await state.initialize(server=api, project_root=tmp_path)
    api.values.catalogue_models.assert_not_awaited()
    assert not state.initialized
    monkeypatch.setenv("SIMSTACK_EXECUTION_ACCOUNT_ID", "test-account")
    await state.initialize(server=api, project_root=tmp_path)
    assert state.initialized
