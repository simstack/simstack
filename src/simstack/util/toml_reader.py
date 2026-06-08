import socket
import sys
import tomllib
from pathlib import Path

from simstack.core.resources import allowed_resources
from simstack.core.route_table import route_table

from simstack.util.path_manager import path_manager
import logging

from simstack.util.transform_file_name import transform_file_name

logger = logging.getLogger(__name__)


class TomlReader:
    def __init__(self, config_path: Path, config_file: Path = Path("simstack.toml")):
        try:
            if config_file.is_absolute():
                toml_file = config_file
            else:
                toml_file = config_path / config_file
            if toml_file.exists():
                with open(toml_file, "rb") as f:
                    self._config = tomllib.load(f)
            else:
                print(f"Config file {toml_file} does not exist. Aborting.")
                sys.exit(-1)
            self._config_path = config_path
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

    def use_db(self):
        return self.get("parameters.general.use_db", False)

    def get_allowed_resources(self):
        allowed_resources_list = self.get("resources.allowed_resources", None)
        if allowed_resources_list is None:
            raise ValueError("Allowed resources must be specified in the config file")
        return allowed_resources_list

    def get_resource_definition(self, resource_str) -> "ResourceDefinition":
        """
        Retrieves the resource definition for the given resource string from the TOML configuration.
        Raises ValueError if the resource is not allowed or if the resource definition is not found.
        Sets the allowed resources list from the config file.
        """
        from simstack.models.resource_definition import ResourceDefinition

        if not allowed_resources.has_resource(resource_str):
            raise ValueError(
                f"Illegal resource {resource_str}. Allowed resources are: {allowed_resources.get_resources()}."
            )

        resource_definition = self.get(f"resources.{resource_str}", None)
        if resource_definition is None:
            raise ValueError(f"Resource definition for {resource_str} not found.")

        resource_definition["resource_str"] = resource_str

        if "workdir" not in resource_definition:
            raise ValueError(f"No workdir specified for resource {resource_str}.")
        else:
            resource_definition["workdir"] = transform_file_name(
                resource_definition["workdir"]
            )

        if "python_paths" not in resource_definition:
            logger.warning(f"No python paths specified for resource {resource_str}.")
        else:
            resource_definition["python_paths"] = [
                transform_file_name(p) for p in resource_definition["python_paths"]
            ]

        if "ssh_key" not in resource_definition:
            logger.warning(f"No ssh key path specified for resource {resource_str}.")
        else:
            resource_definition["ssh_key"] = transform_file_name(
                resource_definition["ssh_key"]
            )

        if "environment_start" not in resource_definition:
            logger.warning(
                f"No environment start command specified for resource {resource_str}."
            )
        if "routes" not in resource_definition:
            logger.warning(f"No routes specified for resource {resource_str}.")
            resource_definition["routes"] = []
        if "hostname" not in resource_definition:
            raise ValueError(f"No hostname specified for resource {resource_str}.")
        elif (
            resource_definition["hostname"] == "test_hostname"
            or resource_definition["hostname"] == "localhost"
        ):
            resource_definition["hostname"] = socket.gethostname()
            logger.info(
                f"Overriding hostname for tests: {resource_definition['hostname']} "
            )

        return ResourceDefinition.model_validate(resource_definition)

    def get_routes(self, resource_str: str):
        return self.get(f"routes.{resource_str}", [])

    def initialize_path_manager(self):
        paths = self.get("paths", {})
        for name, path_info in paths.items():
            if isinstance(path_info, dict) and "path" in path_info:
                path = Path(path_info["path"])
                drops = path_info.get("drops", "")
                path_use_pickle = path_info.get("use_pickle", False)
                path_manager.add_path(name, path, drops, path_use_pickle)

    def build_routes(self):
        route_table.clear_routes()
        for resource_str in allowed_resources.get_resources():
            routes = self.get_routes(resource_str)
            route_table.add_route_set(resource_str, routes)
