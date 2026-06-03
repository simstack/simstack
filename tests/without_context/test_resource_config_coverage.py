import pytest
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from simstack.util.resource_config import ResourceConfig

class MockNodeRunner:
    def __init__(self, name="mock", task_id="NA", logger=None, **kwargs):
        self.name = name
        self.task_id = task_id
        self.logger = logger
        self.subprocess = MagicMock(return_value=True)

@pytest.fixture
def config_file(tmp_path):
    cfg = tmp_path / "config.toml"
    content = """
[local]
os = "windows"

[local.setup]
scripts = ["echo setup1", "echo setup2"]
tmp_base_dir = "set TMP_BASE_DIR=/tmp/base"

[local.post-processing]
scratch_cleanup = true

[local.program.orca]
use_tmp = true
run_command = "orca orca.inp"

[remote]
os = "linux"
[remote.postprocessing]
scratch_cleanup = false
"""
    cfg.write_text(content)
    return cfg

def test_resource_config_init_dir(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    assert rc._resource == "local"
    assert "local" in rc._config

def test_resource_config_init_file(config_file):
    rc = ResourceConfig(config_file, "local")
    assert rc._resource == "local"
    assert "local" in rc._config

def test_resource_config_init_nonexistent(tmp_path):
    rc = ResourceConfig(tmp_path / "nonexistent.toml", "local")
    assert rc._config == {}

def test_os_property(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    assert rc.os == "windows"
    
    rc2 = ResourceConfig(tmp_path, "remote")
    assert rc2.os == "linux"
    
    rc3 = ResourceConfig(tmp_path, "missing")
    assert rc3.os == "linux" # Default

def test_setup_no_runner(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    with patch("subprocess.run") as mock_run:
        rc.setup()
        assert mock_run.call_count == 2
        mock_run.assert_any_call("echo setup1", shell=True, check=True)
        mock_run.assert_any_call("echo setup2", shell=True, check=True)

def test_setup_with_runner(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    runner = MockNodeRunner("mock", "NA")
    rc.setup(node_runner=runner)
    assert runner.subprocess.call_count == 2
    runner.subprocess.assert_any_call("setup_0", "echo setup1")
    runner.subprocess.assert_any_call("setup_1", "echo setup2")

def test_run_with_runner(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    runner = MockNodeRunner()
    
    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            rc.run(node_runner=runner)
            runner.subprocess.assert_called_once()
            call_args, call_kwargs = runner.subprocess.call_args
            assert call_args == ("run", "orca orca.inp")
            assert os.path.samefile(call_kwargs["cwd"], test_cwd)
        finally:
            os.chdir(old_cwd)

def test_run_with_temp_and_copy(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    
    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            test_cwd_path = Path(test_cwd)
            (test_cwd_path / "in.txt").write_text("hello")
            
            config_file.write_text("""
[local.program.orca]
use_temp = true
run_command = "mock_cmd"
input_files = ["in.txt"]
output_files = ["out.txt"]
scratch_cleanup = true
""")
            rc = ResourceConfig(tmp_path, "local")
            rc.program = "orca"

            # Mock subprocess.run to simulate command execution
            def side_effect(*args, **kwargs):
                cwd = kwargs.get("cwd")
                if cwd:
                    (Path(cwd) / "out.txt").write_text("world")
                return MagicMock(returncode=0)

            with patch("subprocess.run", side_effect=side_effect) as mock_run:
                rc.run()
            
            assert (test_cwd_path / "out.txt").exists()
            assert (test_cwd_path / "out.txt").read_text() == "world"
        finally:
            os.chdir(old_cwd)

def test_run_tmp_base_dir_no_assignment(tmp_path, config_file):
    config_file.write_text("""
[local.setup]
tmp_base_dir = "{base_dir}"
[local.program.orca]
use_temp = true
run_command = "echo hi"
""".replace("{base_dir}", (tmp_path / "base").as_posix()))
    
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            base_dir = tmp_path / "base"
            base_dir.mkdir()
            rc.run()
            # Check if a temp dir was created in base
            subdirs = list(base_dir.iterdir())
            assert len(subdirs) >= 1
        finally:
            os.chdir(old_cwd)

def test_get_program(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    params = rc.get_program()
    assert params["run_command"] == "orca orca.inp"
    
    rc.program = "missing"
    assert rc.get_program() == {}

def test_get_setup_params(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    params = rc.get_setup_params()
    assert "scripts" in params

def test_get_postprocessing_params(tmp_path, config_file):
    rc = ResourceConfig(tmp_path, "local")
    assert rc.get_postprocessing_params()["scratch_cleanup"] is True
    
    rc_remote = ResourceConfig(tmp_path, "remote")
    assert rc_remote.get_postprocessing_params()["scratch_cleanup"] is False

def test_run_input_file_missing(tmp_path, config_file):
    # Coverage for if not src.exists()
    config_file.write_text("""
[local.program.orca]
use_temp = true
run_command = "echo"
input_files = ["nonexistent.txt"]
""")
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    with patch("subprocess.run"):
        rc.run()
        # Should not raise error

def test_run_output_file_missing(tmp_path, config_file):
    # Coverage for if not src.exists() for output files
    config_file.write_text("""
[local.program.orca]
use_temp = true
run_command = "echo"
output_files = ["nonexistent_out.txt"]
""")
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"
    with patch("subprocess.run"):
        rc.run()
        # Should not raise error
