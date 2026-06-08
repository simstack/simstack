import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from simstack.util.resource_config import ResourceConfig


class MockFileStack:
    def __init__(self, name, content=None):
        self.name = name
        self.content = content
        self.get = MagicMock()


def test_run_with_parameters_override(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.program.orca]
use_temp = true
run_command = "python -c \\"import shutil; import sys; shutil.copy('param_in.txt', 'param_out.txt')\\""
input_files = ["toml_in.txt"]
output_files = ["toml_out.txt"]
scratch_cleanup = false
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"

    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            test_cwd_path = Path(test_cwd)
            (test_cwd_path / "param_in.txt").write_text("param data")

            # Run with parameter overrides
            rc.run(
                program_name="orca",
                input_files=["param_in.txt"],
                output_files=["param_out.txt"],
            )

            # Check if parameter-specified output is back in cwd
            assert (test_cwd_path / "param_out.txt").exists()
            assert (test_cwd_path / "param_out.txt").read_text() == "param data"

            # Check that TOML specified output does NOT exist (since we overrode it)
            assert not (test_cwd_path / "toml_out.txt").exists()
        finally:
            os.chdir(old_cwd)


def test_run_with_filestack_input(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.program.orca]
use_temp = true
run_command = "python -c \\"import shutil; shutil.copy('fs_in.txt', 'fs_out.txt')\\""
scratch_cleanup = false
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"

    fs_input = MockFileStack("fs_in.txt")

    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            test_cwd_path = Path(test_cwd)

            # Mock fs_input.get to actually create the file in the directory
            def mock_get(local_dir=None):
                target_dir = Path(local_dir) if local_dir else Path.cwd()
                (target_dir / "fs_in.txt").write_text("fs data")

            fs_input.get.side_effect = mock_get

            rc.run(input_files=[fs_input], output_files=["fs_out.txt"])

            assert fs_input.get.called
            assert (test_cwd_path / "fs_out.txt").exists()
            assert (test_cwd_path / "fs_out.txt").read_text() == "fs data"
        finally:
            os.chdir(old_cwd)


def test_run_with_filestack_output_name_handling(tmp_path):
    config_file = tmp_path / "config.toml"
    content = """
[local.program.orca]
use_temp = true
run_command = "python -c \\"open('fs_out.txt', 'w').write('fs result')\\""
"""
    config_file.write_text(content)
    rc = ResourceConfig(tmp_path, "local")
    rc.program = "orca"

    fs_output = MockFileStack("fs_out.txt")

    with tempfile.TemporaryDirectory() as test_cwd:
        old_cwd = os.getcwd()
        os.chdir(test_cwd)
        try:
            test_cwd_path = Path(test_cwd)
            rc.run(output_files=[fs_output])

            assert (test_cwd_path / "fs_out.txt").exists()
            assert (test_cwd_path / "fs_out.txt").read_text() == "fs result"
        finally:
            os.chdir(old_cwd)
