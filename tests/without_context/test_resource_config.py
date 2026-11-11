import pytest
from pathlib import Path
import tomllib
from simstack.util.resource_config import ResourceConfig


@pytest.mark.parametrize("use_temp", [False, True])
@pytest.mark.parametrize("initialized", [False, True])
def test_run_uses_explicit_config_with_real_node_runner(tmp_path, monkeypatch, use_temp, initialized):
    import json
    import shlex
    import sys
    from simstack.core.context import context
    from simstack.core.node_runner import NodeRunner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context, "_initialized", initialized)
    ambient_file = tmp_path / "ambient.toml"
    ambient_file.write_text(
        '[local.setup]\ntmp_base_dir = "ambient-scratch"\n'
        '[local.program.probe]\nrun_command = "exit 99"\nscratch_cleanup = false\n'
    )
    ambient = ResourceConfig(ambient_file, "local")
    monkeypatch.setattr(context, "_resource_config", ambient)
    command = shlex.join([sys.executable, "-c",
        "import os; from pathlib import Path; "
        "Path('result.txt').write_text(Path('input.txt').read_text() + os.environ['PROBE_VALUE'])"])
    explicit_file = tmp_path / "explicit.toml"
    explicit_file.write_text(
        '[local.setup]\ntmp_base_dir = "explicit-scratch"\n'
        '[local.program.probe]\n'
        f'run_command = {json.dumps(command)}\nuse_tmp = {str(use_temp).lower()}\n'
        'scratch_cleanup = true\ninput_files = ["input.txt"]\noutput_files = ["result.txt"]\n'
    )
    config = ResourceConfig(explicit_file, "local")
    (tmp_path / "input.txt").write_text("explicit:")
    runner = NodeRunner("probe", "012345678901234567890123")

    assert config.run("probe", node_runner=runner, env={"PROBE_VALUE": "selected"})

    assert (tmp_path / "result.txt").read_text() == "explicit:selected"
    assert context._resource_config is ambient
    assert context.initialized is initialized
    assert runner.info_files[-1].name == "run.log"
    assert not (tmp_path / "ambient-scratch").exists()
    if use_temp:
        assert (tmp_path / "explicit-scratch").exists()
        assert not list((tmp_path / "explicit-scratch").iterdir())
        assert runner.scratch_dir is None


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
    with pytest.raises(ValueError, match=r"config.toml has no \[local\] resource block"):
        rc.get_program("orca")

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
    with pytest.raises(ValueError, match=r"config.toml has no \[local\] resource block"):
        rc.get_program("any")

def test_resource_config_key_error(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.other]
foo = "bar"
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    with pytest.raises(ValueError, match=r"has no \[program\] table"):
        rc.get_program("orca")
    with pytest.raises(ValueError, match=r"has no \[program\] table"):
        rc.get_program("other")

def test_resource_config_resource_storage(tmp_path):
    rc = ResourceConfig(tmp_path, "remote-resource")
    assert rc._resource == "remote-resource"


def test_same_as_copies_program_setup_and_os(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[int-nano]
os = "linux"
docker_registry = "127.0.0.1:5000"
[int-nano.setup]
tmp_base_dir = "/scratch/int-nano"
[int-nano.post-processing]
scratch_cleanup = true
[int-nano.program.orca]
run_command = "$ORCA_HOME/orca orca.inp > orca.out"

[int-nano-new]
same-as = "int-nano"
"""
    )
    rc = ResourceConfig(tmp_path, "int-nano-new")
    assert rc.get_program("orca")["run_command"] == "$ORCA_HOME/orca orca.inp > orca.out"
    assert rc.get_setup_params()["tmp_base_dir"] == "/scratch/int-nano"
    assert rc.get_postprocessing_params()["scratch_cleanup"] is True
    assert rc.os == "linux"
    assert rc.get_docker_registry() == "127.0.0.1:5000"


def test_same_as_overlay_merges_program_tables(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[int-nano.program.orca]
run_command = "orca-base"
[int-nano.program.vasp]
run_command = "srun vasp_std"

[int-nano-new]
same-as = "int-nano"
[int-nano-new.program.orca]
run_command = "orca-override"
"""
    )
    rc = ResourceConfig(tmp_path, "int-nano-new")
    assert rc.get_program("orca")["run_command"] == "orca-override"
    assert rc.get_program("vasp")["run_command"] == "srun vasp_std"


def test_same_as_chain(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[base.program.orca]
run_command = "from-base"
[mid]
same-as = "base"
[leaf]
same-as = "mid"
"""
    )
    rc = ResourceConfig(tmp_path, "leaf")
    assert rc.get_program("orca")["run_command"] == "from-base"


def test_same_as_missing_target_raises(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[int-nano-new]
same-as = "int-nano"
"""
    )
    rc = ResourceConfig(tmp_path, "int-nano-new")
    with pytest.raises(ValueError, match="does not exist"):
        rc.get_program("orca")


def test_same_as_empty_string_raises(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[int-nano-new]
same-as = ""
"""
    )
    rc = ResourceConfig(tmp_path, "int-nano-new")
    with pytest.raises(ValueError, match="non-empty string"):
        rc.get_program("orca")


def test_same_as_cycle_raises(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[a]
same-as = "b"
[b]
same-as = "a"
"""
    )
    rc = ResourceConfig(tmp_path, "a")
    with pytest.raises(ValueError, match="Circular same-as"):
        rc.get_program("orca")


def test_same_as_underscore_key(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[local.program.orca]
run_command = "orca orca.inp > orca.out"

[local2]
same_as = "local"
"""
    )
    rc = ResourceConfig(tmp_path, "local2")
    assert rc.get_program("orca")["run_command"] == "orca orca.inp > orca.out"


def test_same_as_conflicting_keys_raise(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[local.program.orca]
run_command = "from-local"
[other.program.orca]
run_command = "from-other"

[local2]
same-as = "local"
same_as = "other"
"""
    )
    rc = ResourceConfig(tmp_path, "local2")
    with pytest.raises(ValueError, match="conflicting"):
        rc.get_program("orca")


def test_missing_program_block_raises(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[int-nano.program.orca]
run_command = "orca"
"""
    )
    rc = ResourceConfig(tmp_path, "int-nano")
    with pytest.raises(
        ValueError, match=r"config.toml has no \[int-nano.program.vasp\] block"
    ):
        rc.get_program("vasp")

@pytest.mark.asyncio
async def test_global_state_uses_explicit_machine_configuration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from simstack.core.context import GlobalState
    from simstack.core.resources import allowed_resources
    from simstack.models.resource_definition import ResourceDefinition
    monkeypatch.setattr(GlobalState, "_instance", None)
    monkeypatch.setattr("simstack.server_api.log_upload.setup_runner_logging", lambda *args: None)
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    allowed_resources.clear_resources()
    definition = ResourceDefinition(resource_str="local", hostname="localhost", workdir=str(tmp_path))
    api = SimpleNamespace(resource=SimpleNamespace(bootstrap=AsyncMock(return_value={"account_id": "test-account", "resource": "local", "resources": ["local"], "resource_definition": definition.model_dump(mode="json")})),
        values=SimpleNamespace(catalogue_models=AsyncMock(return_value=[]), catalogue_nodes=AsyncMock(return_value=[])),
        requests=SimpleNamespace(authorization="runner", transport=SimpleNamespace(), server_url="https://example.test/api", token="token"))
    config = tmp_path / "machine.toml"
    config.write_text('[local.program.orca]\ncmd = "run"\n')
    monkeypatch.setenv("SIMSTACK_MACHINE_CONFIG", str(config))
    state = GlobalState()
    await state.initialize(server=api, project_root=tmp_path, resource="local", config_file=config)
    assert state.resource_config._config_path == config
    assert state.resource_config.get_program("orca")["cmd"] == "run"
    allowed_resources.clear_resources()
