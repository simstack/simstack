import stat
import tomllib

from simstack.util.sample_config import sample_config_writer_entrypoint


def test_sample_config_is_api_only_and_retains_machine_profiles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sample_config_writer_entrypoint()
    path = tmp_path / "simstack.toml"
    before = path.read_bytes()
    config = tomllib.loads(before.decode())
    assert config["parameters"]["server"]["url"].endswith("/api")
    assert config["parameters"]["server"]["token"] == "RESOURCE_TOKEN"
    assert "connection_string" not in before.decode() and "mongodb://" not in before.decode()
    assert config["parameters"]["common"]["resources"] == ["local", "int-nano", "horeka", "justus", "self", "exchange", "uploads"]
    assert config["parameters"]["local"]["workdir"] and config["parameters"]["int-nano"]["environment_start"]
    assert len(config["routes"]) == 6 and config["paths"]["models"]["drops"] == "src"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    sample_config_writer_entrypoint()
    assert path.read_bytes() == before
