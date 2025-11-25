import logging
import os
import sys
import tomllib
from typing import List

from simstack.core.config_file import get_config_file
from simstack.util.project_root_finder import find_project_root

logger = logging.getLogger("resources")


class AllowedResources:
    """
    Singleton class that holds a list of allowed resource strings.
    # This class is independent of context because its initialized to an empty list.
    Creating any resource will fail if the config has not been read
    """

    _instance = None
    _resources: List[str] = []

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AllowedResources, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # Only initialize once
        if not hasattr(self, "_initialized"):
            config_file = get_config_file().config_file_path
            config_path = find_project_root()

            try:
                toml_file = os.path.join(config_path, config_file)
                if os.path.exists(toml_file):
                    with open(toml_file, "rb") as f:
                        self.config = tomllib.load(f)
                else:
                    print("simstack.toml file not found in the project root.")
                    sys.exit(-1)
            except tomllib.TOMLDecodeError:
                print("There was an error decoding the TOML file.")
                sys.exit(-1)

            self._resources = (
                self.config.get("parameters", {}).get("common", {}).get("resources", [])
            )
            logger.info(f"Initialized resources to: {self._resources}")
            self._initialized = True

    def set_resources(self, resources: List[str]) -> None:
        """Set the list of allowed resources."""
        self._resources = resources.copy() if resources else []

    def get_resources(self) -> List[str]:
        """Get the list of allowed resources."""
        return self._resources.copy()

    def add_resource(self, resource: str) -> None:
        """Add a single resource to the list."""
        if resource not in self._resources:
            self._resources.append(resource)

    def remove_resource(self, resource: str) -> None:
        if resource == "self":
            raise ValueError("Cannot remove 'self' resource")
        """Remove a resource from the list."""
        if resource in self._resources:
            self._resources.remove(resource)

    def clear_resources(self) -> None:
        """Clear all resources from the list."""
        self._resources.clear()

    def has_resource(self, resource: str) -> bool:
        """Check if a resource exists in the list."""
        return resource in self._resources

    def __len__(self) -> int:
        """Return the number of resources."""
        return len(self._resources)

    def __iter__(self):
        """Make the class iterable."""
        return iter(self._resources)

    def __contains__(self, resource: str) -> bool:
        """Support 'in' operator."""
        return resource in self._resources

    def __str__(self) -> str:
        """String representation."""
        return f"AllowedResources({self._resources})"

    def __repr__(self) -> str:
        """String representation."""
        return f"AllowedResources({self._resources!r})"


allowed_resources = AllowedResources()
