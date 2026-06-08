import os
from pathlib import Path

from simstack.core.context import context


# Get the module name from the current file path
def get_module_path(file_path: str):
    # Get the absolute path of the current file
    file_path = Path(file_path).resolve()
    # If not found in sys.path, use an alternative approach
    project_root = context.config.project_root  # Assumes running from project root
    try:
        relative_path = file_path.relative_to(project_root)

        module_path = str(relative_path.with_suffix("")).replace(os.sep, ".")
        return module_path
    except ValueError:
        return None
