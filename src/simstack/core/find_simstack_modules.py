import importlib
import pkgutil
from importlib.metadata import entry_points
from logging import getLogger

logger = getLogger("find_simstack_modules")


def discover_simstack_modules():
    """Discover all packages and modules within the simstack package."""


def find_simstack_modules():
    """Find all packages and modules within the simstack package."""

    all_modules = []

    def walk_packages(package_name):
        """Walk through all packages and modules."""
        try:
            package = importlib.import_module(package_name)
            package_path = package.__path__

            logger.info(f"Package: {package_name}")
            # print(f"Path: {package_path}")

            for importer, modname, ispkg in pkgutil.walk_packages(
                package_path, prefix=f"{package_name}."
            ):
                if ispkg:
                    logger.debug(f"  Subpackage: {modname}")
                else:
                    logger.debug(f"  Module: {modname}")
                    # Split module name by periods
                    all_modules.append(modname)

        except Exception as e:
            logger.error(f"Error walking {package_name}: {e}")

    entry_point_list = entry_points(group="simstack.modules")
    for entry_point in entry_point_list:
        walk_packages(entry_point.value)
    return all_modules
