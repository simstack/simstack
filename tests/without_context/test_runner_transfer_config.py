from types import SimpleNamespace
import pytest
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.config_reader import ConfigReader
from simstack.util.file_transfer_client import FileTransferClient

@pytest.mark.parametrize("config_token,env_name,expected", [
    (None, None, None),
    ("configured", None, "configured"),
    (None, "SIMSTACK_RUNNER_TOKEN", "enrolled"),
    (None, "SIMSTACK_SERVER_TOKEN", "enrolled"),
    ("configured", "SIMSTACK_RUNNER_TOKEN", "enrolled"),
])
def test_transfer_credentials_are_explicit(tmp_path, monkeypatch, caplog, config_token, env_name, expected):
    for name in ("SIMSTACK_RUNNER_TOKEN", "SIMSTACK_SERVER_TOKEN", "SIMSTACK_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)
    if env_name:
        monkeypatch.setenv(env_name, "enrolled")
    definition = ResourceDefinition(resource_str="local", hostname="localhost", workdir=str(tmp_path))
    config = ConfigReader(definition, project_root=tmp_path,
        server_url="https://example.test/api", server_token=config_token, account_id="test-account")
    monkeypatch.setattr("simstack.core.context.context", SimpleNamespace(config=config))
    client = FileTransferClient.from_context(required=False)
    if expected is None:
        assert client is None
    else:
        assert client.runner_token == expected
        assert client.server_url == "https://example.test/api"
        assert expected not in caplog.text
    assert "runner_access_token" not in definition.model_dump()
