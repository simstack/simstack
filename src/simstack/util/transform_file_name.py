import os
from pathlib import Path
from typing import Optional

from simstack.util.project_root_finder import find_project_root

def transform_file_name(path_str: str | Path) -> Path:
    """
    Transform a string path by substituting environment variables.

    Args:
        path_str: String containing path with optional environment variables
                ($HOME, $PROJECT, $TEMP)

    Returns:
        Path object with resolved environment variables

    Raises:
        FileNotFoundError: If the resolved path does not exist
    """

    if isinstance(path_str, Path):
        path_str = str(path_str)

    replacements = {
        "$HOME": os.environ.get("HOME", str(Path.home())),
        "$TEMP": os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
        "$PROJECT": os.environ.get("PROJECT", str(find_project_root()))
    }


    for var, value in replacements.items():
        path_str = path_str.replace(var, value)

    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return path

class TransformedPath(Path):
    def __init__(self, path_str: str):
        super().__init__(transform_file_name(path_str))