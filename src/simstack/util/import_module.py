import importlib
import logging
import os
import sys
from pathlib import Path

from simstack.util.project_root_finder import find_project_root

logger = logging.getLogger("import_module_from_file")


def import_module_from_file(file_path: Path):
    """
    Import a Python file as a module.

    Args:
        file_path: Path object pointing to the Python file to import

    Returns:
        Imported module or None if import failed
    """
    try:
        logger.debug(f"Attempting to import module from: {file_path}")
        if not file_path.exists():
            print(f"File not found: {file_path}")
            return []

        root_dir = find_project_root()
        relative_path = file_path.relative_to(root_dir)
        directory, filename = os.path.split(str(relative_path))
        basename = filename.split(".")[0]

        module_name = ".".join(relative_path.parts[:-1]) + "." + basename

        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
            logger.debug(f"Added {root_dir} to sys.path")
        # Try simple import first
        try:
            module = importlib.import_module(module_name)
            return module
        except ImportError as e:
            logger.error(f"Direct import failed: {e}")

            # Fall back to spec-based import
            spec = importlib.util.spec_from_file_location(module_name, str(file_path))
            if spec is None or spec.loader is None:
                print(f"Failed to create spec for {file_path}")
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
                return module
            except Exception as e:
                logger.error(f"Error processing module: {file_path}  {e}")
                return None

    except Exception as e:
        logger.error(f"Error importing module from {file_path}: {e}")
        import traceback

        traceback.print_exc()
        return None
