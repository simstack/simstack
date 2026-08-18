import importlib
import logging
import sys
from pathlib import Path

logger = logging.getLogger("import_module_from_file")


def import_module_from_file(file_path: Path, root_dir: Path):
    """
    Import a Python file as a module.

    Args:
        root_dir:
        file_path: Path object pointing to the Python file to import

    Returns:
        Imported module or None if import failed
    """
    try:
        # logger.debug(f"Attempting to import module from: {file_path}")
        if not file_path.exists():
            print(f"File not found: {file_path}")
            return []

        relative_path = file_path.relative_to(root_dir)

        basename = relative_path.stem
        package_path = ".".join(relative_path.parts[:-1])
        module_name = f"{package_path}.{basename}" if package_path else basename

        if root_dir not in sys.path:
            sys.path.insert(0, str(root_dir))
            # logger.debug(f"Added {root_dir} to sys.path")
        # Try a simple import first
        try:
            if module_name is None or module_name == "":
                raise ImportError(f"Module name is None for file: {file_path}")
            module = importlib.import_module(module_name)
            return module
        except ImportError as e:
            logger.warning(f"Direct import failed: {e}  -- Trying spec-based import")

            # Fall back to spec-based import
            spec = importlib.util.spec_from_file_location(module_name, str(file_path))
            if spec is None or spec.loader is None:
                logger.error(f"Failed to create spec for {file_path}")
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
                return module
            except Exception as e:
                logger.error(f"Import error when processing module: {file_path}  {e}")
                return None

    except Exception as e:
        logger.error(f"Error importing module from {file_path}: {e}")
        import traceback

        traceback.print_exc()
        return None
