import os
from pathlib import Path

from simstack.util.project_root_finder import find_project_root


def transform_file_name(path_str: str | Path, project_root: Path | None = None) -> Path:
    """
    Transform a string path by substituting environment variables.

    Args:
        path_str: String containing path with optional environment variables
                ($HOME, $PROJECT, $TEMP)
        project_root: Optional project root directory. If not provided, it will be automatically detected.

    Returns:
        Path object with resolved environment variables

    Raises:
        FileNotFoundError: If the resolved path does not exist
    """

    if isinstance(path_str, Path):
        path_str = str(path_str)

    if project_root is None:
        project_root = find_project_root()

    replacements = {
        "$HOME": os.environ.get("HOME", str(Path.home())),
        "$TEMP": os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
        "$PROJECT": os.environ.get("PROJECT", str(project_root)),
    }

    for var, value in replacements.items():
        path_str = path_str.replace(var, value)

    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return path
