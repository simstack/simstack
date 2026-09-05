import importlib
import pkgutil
from importlib.metadata import entry_points
from logging import getLogger

logger = getLogger("find_modules")


def walk_packages(package_name, all_modules):
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


def find_simstack_modules():
    """Find all packages and modules within the simstack package."""

    all_modules = []

    entry_point_list = entry_points(group="simstack.modules")
    if not entry_point_list:
        logger.warning("No simstack.modules entry points found.")
        return all_modules

    logger.warning("simstack.modules entry points:")
    for entry_point in entry_point_list:
        logger.warning("  %s = %s", entry_point.name, entry_point.value)
        walk_packages(entry_point.value, all_modules)
    return all_modules
