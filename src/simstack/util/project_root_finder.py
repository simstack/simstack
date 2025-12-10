import os
from pathlib import Path


def find_project_root(current_file=None, marker_files=(".git", "simstack.toml", "setup.py")) -> Path:
    """
    Find the project root directory by searching for common marker files

    Args:
        current_file: Path to the current file, defaults to __file__ if None
        marker_files: Tuple of files/directories that indicate the project root

    Returns:
        Absolute path to the project root directory
    """
    if current_file is None:
        current_file = __file__

    # Get the directory of the current file
    current_dir = Path(current_file).resolve().parent

    # Walk up the directory tree until we find a marker file
    prev_dir = None
    while current_dir != prev_dir:
        # Check if any marker files/directories exist in the current directory
        for marker in marker_files:
            if (current_dir / marker).exists() and not (current_dir / "simstack_marker.com").exists():
                return current_dir

        # Move up one directory
        prev_dir = current_dir
        current_dir = current_dir.parent

    # If we can't find any markers, return the directory of the current file
    return Path(current_file).resolve().parent
