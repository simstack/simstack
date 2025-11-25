import os
from typing import List


def find_python_files(directory: str) -> List[str]:
    """
    Returns full paths of all .py files in the given directory and its subdirectories,
    excluding __init__.py files.

    Args:
        directory: The root directory to search from

    Returns:
        List of absolute file paths to all .py files that aren't __init__.py
    """
    python_files = []

    # Ensure the directory exists
    if not os.path.isdir(directory):
        raise ValueError(f"'{directory}' is not a valid directory")

    # Convert to absolute path
    directory = os.path.abspath(directory)

    # Walk through directory structure
    for root, dirs, files in os.walk(directory):
        for file in files:
            # Check if it's a Python file but not __init__.py
            if file.endswith(".py") and file != "__init__.py":
                full_path = os.path.join(root, file)
                python_files.append(full_path)

    return python_files
