from pathlib import Path
import pytest
from simstack.core.resources import allowed_resources
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.config_reader import ConfigReader

@pytest.fixture(autouse=True)
def resources():
    allowed_resources.clear_resources()
    yield
    allowed_resources.clear_resources()

def test_resolved_resource_preserves_machine_settings(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    source = tmp_path / "python"
    source.mkdir()
    key = tmp_path / "key"
    key.touch()
    definition = ResourceDefinition(resource_str="local", hostname="machine",
        workdir=str(work), python_paths=[str(source)], ssh_key=str(key),
        environment_start="source environment.sh", routes=["uploads"])
    config = ConfigReader(definition, project_root=tmp_path,
        server_url="https://example.test/api", server_token="private-token", account_id="test-account")
    assert config.workdir == work
    assert config.project_root == tmp_path
    assert config.python_paths == [source]
    assert config.ssh_key == key
    assert config.environment_start == "source environment.sh"
    assert config.hostname == "machine"
    assert str(config.resource) == "local"
    assert definition.routes == ["uploads"]
    assert not hasattr(config, "connection_string")
    assert not hasattr(config, "db_type")
    with pytest.raises(AttributeError):
        config.resource = "other"
