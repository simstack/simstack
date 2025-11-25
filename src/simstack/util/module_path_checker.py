import os
from pathlib import Path

from simstack.util.project_root_finder import find_project_root


def is_module_subpath_of_path(module_path: str, path_info_path: str) -> bool:
    """
    Check whether a dot-separated module path is a subpath of an absolute path.

    Args:
        module_path: A dot-separated path relative to the project root (e.g., "src.simstack.core.artifacts")
        path_info_path: An absolute path below the project root (e.g., "/path/to/project/src/simstack/core")

    Returns:
        True if the module path corresponds to a location under the given absolute path, False otherwise
    """

    # Convert the module path to file system path
    # Replace dots with path separators
    module_file_path = module_path.replace(".", os.sep)

    # Convert path_info_path to Path object for easier manipulation
    path_info_path_obj = Path(path_info_path)

    # Get the project root using the existing utility function
    project_root = Path(find_project_root())

    # Convert module path to absolute path relative to project root
    module_absolute_path = project_root / module_file_path

    try:
        # Check if the module path is under the given path_info_path
        module_absolute_path.relative_to(path_info_path_obj)
        return True
    except ValueError:
        # If relative_to raises ValueError, the module path is not under path_info_path
        return False
