import pytest
from pathlib import Path
import os
import shutil
import tempfile
from simstack.util.resource_config import ResourceConfig

def test_resource_config_os_property(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local]
os = "windows"
[linux_res]
# No os specified
"""
    config_file.write_text(content)
    
    rc_local = ResourceConfig(tmp_path, "local")
    assert rc_local.os == "windows"
    
    rc_linux = ResourceConfig(tmp_path, "linux_res")
    assert rc_linux.os == "linux"
    
    rc_none = ResourceConfig(tmp_path, "non_existent")
    assert rc_none.os == "linux"

def test_resource_config_setup(tmp_path, monkeypatch):
    config_file = tmp_path / "config.toml"
    # Create a dummy script file
    script_file = tmp_path / "test_script.py"
    script_file.write_text("print('hello from script')")
    
    content = f"""
[local.setup]
scripts = ["python {script_file.as_posix()}"]
"""
    config_file.write_text(content)
    
    rc = ResourceConfig(tmp_path, "local")
    # This should execute the script without error
    rc.setup()

def test_resource_config_run_without_temp(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.program.test_prog]
use_temp = false
run_command = "python -c \\"import shutil; shutil.copy('input.txt', 'output.txt')\\""
input_files = ["input.txt"]
output_files = ["output.txt"]
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    
    # Change to a temporary directory for the test to avoid polluting project root
    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            test_cwd_path = Path(test_cwd)
            # Create an input file
            (test_cwd_path / "input.txt").write_text("input data")
            
            # Run
            rc.run(program_name="test_prog")
            
            assert (test_cwd_path / "output.txt").exists()
            assert (test_cwd_path / "output.txt").read_text() == "input data"
        finally:
            os.chdir(old_cwd)

def test_resource_config_run_with_temp(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.program.test_prog]
use_temp = true
run_command = "python -c \\"import shutil; shutil.copy('input.txt', 'output.txt')\\""
input_files = ["input.txt"]
output_files = ["output.txt"]
# scratch_cleanup = true # Wait, I will use setup_params to mock base dir
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    
    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            test_cwd_path = Path(test_cwd)
            (test_cwd_path / "input.txt").write_text("input data")
            
            # We will use a separate base dir for temp
            temp_base = test_cwd_path / "my_temp_base"
            temp_base.mkdir()
            
            # Add tmp_base_dir to config via setup or directly in program if supported
            # In our current implementation of run, it checks program then setup.
            rc.get_setup_params = lambda: {"tmp_base_dir": str(temp_base)}
            
            # Re-enable scratch_cleanup for this test case
            content_with_cleanup = content.replace("# scratch_cleanup = true", "scratch_cleanup = true")
            config_file.write_text(content_with_cleanup)
            rc = ResourceConfig(tmp_path, "local")
            rc.get_setup_params = lambda: {"tmp_base_dir": str(temp_base)}

            rc.run(program_name="test_prog")
            
            # Output should be back in cwd
            assert (test_cwd_path / "output.txt").exists()
            assert (test_cwd_path / "output.txt").read_text() == "input data"
            
            # Temp dir should be cleaned up
            remaining = list(temp_base.iterdir())
            assert len(remaining) == 0
            
        finally:
            os.chdir(old_cwd)

def test_resource_config_tmp_base_dir_expansion(tmp_path, monkeypatch):
    # Test that tmp_base_dir handles environment variable expansion and directory creation
    rc = ResourceConfig(tmp_path, "test")
    
    with tempfile.TemporaryDirectory() as test_cwd:
        test_cwd_path = Path(test_cwd)
        # 1. Test normal path (non-existent)
        temp_base = test_cwd_path / "new_base"
        assert not temp_base.exists()
        rc.get_setup_params = lambda: {"tmp_base_dir": str(temp_base)}
        
        path = rc.tmp_base_dir
        assert path == temp_base
        assert temp_base.exists()
        
        # 2. Test environment variable expansion
        env_base = test_cwd_path / "env_base"
        monkeypatch.setenv("MY_TEMP_VAR", str(env_base))
        assert not env_base.exists()
        rc.get_setup_params = lambda: {"tmp_base_dir": "$MY_TEMP_VAR" if os.name != 'nt' else "%MY_TEMP_VAR%"}
        
        path = rc.tmp_base_dir
        assert path == env_base
        assert env_base.exists()

        # 3. Test user expansion (~)
        # We'll just check if it calls expanduser by mocking it or checking results
        # A simple check: if we use ~ it should not be literally ~
        rc.get_setup_params = lambda: {"tmp_base_dir": "~/simstack_tmp_test"}
        path = rc.tmp_base_dir
        assert path != Path("~/simstack_tmp_test")
        assert path.is_absolute()
        # Clean up the created directory if it's in home
        if path.exists() and "simstack_tmp_test" in path.name:
             shutil.rmtree(path)
