from pathlib import Path
import os
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
    rc.program = "test_prog"

    # Change to a temporary directory for the test to avoid polluting project root
    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            test_cwd_path = Path(test_cwd)
            # Create an input file
            (test_cwd_path / "input.txt").write_text("input data")

            # Run
            rc.run()

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
scratch_cleanup = true
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "test_prog"

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

            rc.run()

            # Output should be back in cwd
            assert (test_cwd_path / "output.txt").exists()
            assert (test_cwd_path / "output.txt").read_text() == "input data"

            # Temp dir should be cleaned up
            remaining = list(temp_base.iterdir())
            assert len(remaining) == 0

        finally:
            os.chdir(old_cwd)


def test_resource_config_run_tmp_base_dir_assignment(tmp_path):
    # Test that tmp_base_dir handles assignments like "set TMP_BASE_DIR=..."
    config_file = tmp_path / "config.toml"
    content = """
[local.program.test_prog]
use_temp = true
run_command = "python -c \\"open('output.txt', 'w').write('done')\\""
output_files = ["output.txt"]
scratch_cleanup = false
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "test_prog"

    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            test_cwd_path = Path(test_cwd)
            temp_base = test_cwd_path / "my_assigned_temp_base"
            # Simulate a windows style set command in the string
            tmp_base_dir_str = f"set TMP_BASE_DIR={temp_base}"

            rc.get_setup_params = lambda: {"tmp_base_dir": tmp_base_dir_str}

            rc.run()

            assert (test_cwd_path / "output.txt").exists()
            assert temp_base.exists()
            # Since scratch_cleanup=False, there should be a temp dir inside temp_base
            subdirs = [d for d in temp_base.iterdir() if d.is_dir()]
            assert len(subdirs) == 1
        finally:
            os.chdir(old_cwd)
