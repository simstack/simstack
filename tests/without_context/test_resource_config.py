import pytest
from pathlib import Path
import tomllib
from simstack.util.resource_config import ResourceConfig

def test_resource_config_basic(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[local.program.orca]\nrun_command = \"orca orca.inp\"\n")
    rc = ResourceConfig(tmp_path, "local")
    params = rc.get_program("orca")
    assert params["run_command"] == "orca orca.inp"
    assert rc._resource == "local"


def test_resource_config_reload_picks_up_docker_image(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[local.program.psi4_calculator]\nrun_command = \"psi4\"\n")
    rc = ResourceConfig(tmp_path, "local")
    assert "docker_image" not in rc.get_program("psi4_calculator")

    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
    )
    assert "docker_image" not in rc.get_program("psi4_calculator")

    rc.reload()
    assert rc.get_program("psi4_calculator")["docker_image"] == "molecular-qm-psi4:latest"


def test_resource_config_reload_missing_file_clears_config(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[local.program.orca]\nrun_command = \"orca\"\n")
    rc = ResourceConfig(tmp_path, "local")
    assert rc.get_program("orca")["run_command"] == "orca"

    config_file.unlink()
    rc.reload()
    assert rc.get_program("orca") == {}

def test_resource_config_from_file(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.program.orca]
run_command = "orca orca.inp"
"""
    config_file.write_text(content)
    
    # Test passing directory
    rc = ResourceConfig(tmp_path, "local")
    params = rc.get_program("orca")
    assert params["run_command"] == "orca orca.inp"
    
    # Test passing file path
    rc2 = ResourceConfig(config_file, "local")
    params2 = rc2.get_program("orca")
    assert params2["run_command"] == "orca orca.inp"

def test_resource_config_missing():
    rc = ResourceConfig(Path("non_existent_path"), "local")
    params = rc.get_program("any")
    assert params == {}

def test_resource_config_key_error(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.other]
foo = "bar"
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    assert rc.get_program("orca") == {}
    
    assert rc.get_program("other") == {} # Though it's under local.other, get_program expects local.program.other

def test_resource_config_resource_storage(tmp_path):
    rc = ResourceConfig(tmp_path, "remote-resource")
    assert rc._resource == "remote-resource"

@pytest.mark.asyncio
async def test_global_state_initialization(tmp_path):
    from simstack.core.context import GlobalState
    from simstack.util.db import DBType
    from simstack.models.resource_definition import ResourceDefinition
    
    gs = GlobalState()
    # Ensure it's not initialized
    gs._initialized = False
    
    project_root = tmp_path
    (project_root / "simstack.toml").write_text("[parameters.db]\ndatabase = \"test_db\"\n")
    config_file = project_root / "config.toml"
    config_file.write_text("[local.program.orca]\ncmd = \"run\"\n")
    
    # Mimic conftest.py:
    # 1. Initialize with skip_config=True
    await gs.initialize(
        is_test=True,
        project_root=project_root,
        db_name="test_db",
        connection_string="mongodb://localhost:27017", # Provide a dummy connection string to satisfy from_config
        db_type=DBType.IN_MEMORY,
        resource="local",
        skip_config=True
    )
    
    # 2. Save ResourceDefinition to DB
    # We need to patch the DB save because mongomock doesn't support sessions
    # mimicking create_db_patches from conftest.py
    async def patched_save(instance, *args, **kwargs):
        collection = gs.db.get_collection(type(instance))
        if not instance.id:
            from odmantic import ObjectId
            instance.id = ObjectId()
        doc = instance.model_dump(by_alias=True)
        doc["_id"] = instance.id
        await collection.replace_one({"_id": instance.id}, doc, upsert=True)
        return instance

    gs.db._engine.save = patched_save
    
    resource_definition = ResourceDefinition(
        resource_str="local",
        workdir=str(tmp_path),
        hostname="localhost",
        is_default=True
    )
    await gs.db.save(resource_definition)
    
    # 3. Initialize configs
    # initialize_configs calls ConfigReader.create which calls initialize_resource_from_db
    from simstack.util.toml_reader import TomlReader
    mock_toml = AsyncMock(spec=TomlReader)
    mock_toml.get.return_value = None
    
    await gs.initialize_configs(
        gs.db, 
        mock_toml,
        project_root=project_root,
        resource="local",
        workdir=tmp_path,
        python_paths=[project_root]
    )
    
    assert hasattr(gs, "resource_config")
    assert gs.resource_config._resource == "local"
    params = gs.resource_config.get_program("orca")
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
