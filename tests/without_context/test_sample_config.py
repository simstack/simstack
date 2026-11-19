import tomllib

from simstack.util.sample_config import sample_config_writer_entrypoint
from simstack.util.resource_config import ResourceConfig
from tests.support.file_permissions import assert_private_file


def test_sample_config_defines_a_portable_local_runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sample_config_writer_entrypoint()
    path = tmp_path / "simstack.toml"
    before = path.read_bytes()
    config = tomllib.loads(before.decode())
    assert config["parameters"]["server"]["url"].endswith("/api")
    assert "token" not in config["parameters"]["server"]
    assert "mongodb://" not in before.decode()
    assert '$HOME/.local/share/simstack/runners/<slug>' in before.decode()
    assert '--credentials "$runner_dir/credentials.json"' in before.decode()
    profile = ResourceConfig(path, "workstation")
    assert profile.get_program("python")["run"] == "python --version"
    assert_private_file(path)
    sample_config_writer_entrypoint()
    assert path.read_bytes() == before
