import importlib
import tomllib
from pathlib import Path

import pytest

pytest.mark.skip(reason="If there are non-simstack directories in the pyproject.toml, this test will fail.")
def test_console_script_targets_are_importable():
    pyproject_path = Path(__file__).parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        scripts = tomllib.load(pyproject_file)["project"]["scripts"]

    for command, target in scripts.items():
        module_name, attribute_name = target.split(":", maxsplit=1)
        try:
            module = importlib.import_module(module_name)
            assert callable(getattr(module, attribute_name)), command
        except ImportError:
            pass