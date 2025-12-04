import sys
import tomllib
from pathlib import Path

from simstack.util.project_root_finder import find_project_root


class TomlReader:
    def __init__(self, config_path: Path = None, config_file: str = "simstack.toml"):
        if config_path is None:
            config_path = Path(find_project_root())
        try:
            toml_file = config_path / config_file
            if toml_file.exists():
                with open(toml_file, "rb") as f:
                    self._config = tomllib.load(f)
            else:
                print(f"Config file {toml_file} does not exist. Aborting.")
                sys.exit(-1)
        except tomllib.TOMLDecodeError:
            print("There was an error decoding the TOML file.")
            sys.exit(-1)

    @property
    def config(self):
        return self._config

    def get(self, key: str, default=None):
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value