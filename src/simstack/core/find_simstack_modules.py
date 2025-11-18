import importlib
import pkgutil
from logging import getLogger

logger = getLogger("find_simstack_modules")

def find_simstack_modules():
    """Find all packages and modules within the simstack package."""

    all_modules = []

    def walk_packages(package_name):
        """Walk through all packages and modules."""
        try:
            package = importlib.import_module(package_name)
            package_path = package.__path__

            print(f"Package: {package_name}")
            print(f"Path: {package_path}")

            for importer, modname, ispkg in pkgutil.walk_packages(
                    package_path,
                    prefix=f"{package_name}."
            ):
                if ispkg:
                    logger.debug(f"  Subpackage: {modname}")
                else:
                    logger.debug(f"  Module: {modname}")
                    # Split module name by periods
                    splitext = modname.split('.')
                    if len(splitext) > 1 and (splitext[1] == "models" or splitext[1] == "methods"):
                      all_modules.append(modname)


        except Exception as e:
            logger.error(f"Error walking {package_name}: {e}")

    walk_packages('simstack')
    return all_modules
