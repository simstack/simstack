import pytest
from pathlib import Path
from simstack.util.resource_config import ResourceConfig


def test_resource_config_basic(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[local.program.orca]\nrun_command = "orca orca.inp"\n')
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    params = rc.get_program()
    assert params["run_command"] == "orca orca.inp"
    assert rc._resource == "local"
    assert rc._program == "orca"


def test_resource_config_from_file(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.program.orca]
run_command = "orca orca.inp"
"""
    config_file.write_text(content)

    # Test passing directory
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    params = rc.get_program()
    assert params["run_command"] == "orca orca.inp"

    # Test passing file path
    rc2 = ResourceConfig(config_file, "local")
    rc2.program = "orca"
    params2 = rc2.get_program()
    assert params2["run_command"] == "orca orca.inp"


def test_resource_config_missing():
    rc = ResourceConfig(Path("non_existent_path"), "local")
    rc.program = "any"
    params = rc.get_program()
    assert params == {}


def test_resource_config_key_error(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.other]
foo = "bar"
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    assert rc.get_program() == {}

    rc.program = "other"  # Though it's under local.other, get_program expects local.program.other
    assert rc.get_program() == {}


def test_resource_config_resource_storage(tmp_path):
    rc = ResourceConfig(tmp_path, "remote-resource")
    assert rc._resource == "remote-resource"
    assert rc._program is None


@pytest.mark.asyncio
async def test_global_state_initialization(tmp_path, monkeypatch):
    from simstack.core.context import GlobalState
    from simstack.util.db import Database, DBType
    from simstack.util.config_reader import ConfigReader
    from simstack.util.resource_config import ResourceConfig

    # Mock Database and ConfigReader
    mock_db = AsyncMock()
    monkeypatch.setattr(Database, "from_db_info", lambda info: mock_db)
    monkeypatch.setattr(
        ConfigReader, "create", AsyncMock(return_value=SimpleNamespace())
    )

    # We need to mock initialize_logging and refresh_mappings to avoid side effects
    monkeypatch.setattr(
        GlobalState,
        "initialize_logging",
        lambda self, connection_string, db_name, is_test, log_level: None,
    )
    monkeypatch.setattr(GlobalState, "refresh_mappings", AsyncMock())

    gs = GlobalState()
    # Ensure it's not initialized
    gs._initialized = False

    config_file = tmp_path / "config.toml"
    config_file.write_text('[local.program.orca]\ncmd = "run"\n')

    await gs.initialize(
        is_test=True,
        project_root=tmp_path,
        db_name="test",
        connection_string="test",
        db_type=DBType.IN_MEMORY,
        resource="local",
    )

    assert hasattr(gs, "resource_config")
    assert isinstance(gs.resource_config, ResourceConfig)
    assert gs.resource_config._resource == "local"
    gs.resource_config.program = "orca"
    params = gs.resource_config.get_program()
    assert params == {"cmd": "run"}


def test_resource_config_setup_and_postprocessing(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.setup]
scripts = ["echo setup"]
tmp_base_dir = "/tmp"

[local.post-processing]
scratch_cleanup = true

[remote.postprocessing]
scratch_cleanup = false
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")

    # Test setup
    setup = rc.get_setup_params()
    assert setup["scripts"] == ["echo setup"]
    assert setup["tmp_base_dir"] == "/tmp"

    # rc_missing = ResourceConfig(tmp_path, "non-existent")
    # setup_missing = rc_missing.get_setup_params()
    # assert setup_missing == {}

    # Test post-processing (with dash)
    post = rc.get_postprocessing_params()
    assert post["scratch_cleanup"] is True

    # Test postprocessing (without dash)
    rc_remote = ResourceConfig(tmp_path, "remote")
    post_remote = rc_remote.get_postprocessing_params()
    assert post_remote["scratch_cleanup"] is False

    # Test non-existent
    rc_none = ResourceConfig(tmp_path, "non-existent")
    assert rc_none.get_postprocessing_params() == {}


from unittest.mock import AsyncMock
from types import SimpleNamespace
