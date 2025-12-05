import os
import sys
import tomllib
from pathlib import Path

from simstack.core.resources import allowed_resources
from simstack.util.path_manager import PathManager, path_manager
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

    def get_resource_definition(self, resource_str):
        """
        Retrieves the resource definition for the given resource string from the TOML configuration.
        Raises ValueError if the resource is not allowed or if the resource definition is not found.
        Sets the allowed resources list from the config file.
        """
        allowed_resources_list = self.get("parameters.common.allowed_resources", None)
        allowed_resources.set_resources(allowed_resources_list)
        if not allowed_resources.has_resource(resource_str):
            raise ValueError("Allowed resources must be specified in the config file")

        resource_definition = self.get(f"parameters.{resource_str}", None)
        if resource_definition is None:
            raise ValueError(f"Resource definition for {resource_str} not found")
        return resource_definition

    def initialize_path_manager(self):
        paths = self.get("paths", {})
        for name, path_info in paths.items():
            if isinstance(path_info, dict) and "path" in path_info:
                path = path_info["path"]
                drops = path_info.get("drops", "")
                path_use_pickle = path_info.get("use_pickle", False)
                path_manager.add_path(name, path, drops, path_use_pickle)
