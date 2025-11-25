import os

from simstack.util.project_root_finder import find_project_root


# Get the module name from the current file path
def get_module_path(file_path: str):
    # Get the absolute path of the current file
    file_path = os.path.abspath(file_path)

    # # Get all directories in the Python path
    # for path in sys.path:
    #     if path and file_path.startswith(path):
    #         # Remove the path prefix and the .py extension
    #         relative_path = file_path[len(path):].lstrip(os.sep)
    #         module_path = os.path.splitext(relative_path)[0].replace(os.sep, '.')
    #         return module_path

    # If not found in sys.path, use an alternative approach
    project_root = find_project_root()  # Assumes running from project root
    if file_path.startswith(project_root):
        relative_path = file_path[len(project_root) :].lstrip(os.sep)
        module_path = os.path.splitext(relative_path)[0].replace(os.sep, ".")
        return module_path

    return None
