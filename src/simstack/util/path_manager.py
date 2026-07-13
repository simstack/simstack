from pathlib import Path
from typing import List, Dict, Optional, Iterator, Any, Union

from simstack.util.directory_iterator import DirectoryPath
from simstack.util.project_root_finder import find_project_root


class PathManager:
    """
    Manages paths for the SimStack application, providing mechanisms to find Python files
    for nodes and models. Will read only .py files.
    By default travers all directories below the project root.
    Uses DirectoryPath for efficient directory traversal.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(PathManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def default_excludes(self):
        return [
            "__pycache__",
            "*.pyc",
            ".git",
            ".venv",
            "venv",
        ]

    def reset(self):
        self._initialized = False
        self.paths.clear()
        self._excluded_patterns = self.default_excludes()

    def __init__(self, use_pickle: bool = False, include_project_root: bool = False):
        """
        Initialize the PathManager.

        Args:
            use_pickle: Boolean flag indicating whether to use pickle for serialization
        """
        if self._initialized:
            return

        self._initialized = True
        self.use_pickle = use_pickle
        self.root_dir = find_project_root()
        self.paths: Dict[str, Dict[str, Union[Path, str, bool]]] = {}

        if include_project_root:
            self.add_path("project_root", self.root_dir)

        self._excluded_patterns = self.default_excludes()

    def add_path(self, name: str, path: Path, drops: str = "", use_pickle: bool = False) -> None:
        """
        Add a path to the manager.

        Args:
            name: Name identifier for the path
            path: The directory path relative to the project root
            drops: Prefix to drop from module names (for import paths)
            use_pickle: Whether to use pickle for this path
        """
        # Convert relative paths to absolute paths
        if not path.is_absolute():
            path = self.root_dir / path

        if not path.is_dir():
            raise ValueError(f"'{path}' is not a valid directory")

        self.paths[name] = {"path": path, "drops": drops, "use_pickle": use_pickle}

    def get_path(self, name: str) -> Dict[str, str]:
        """
        Get a path by name.

        Args:
            name: Name of the path to retrieve

        Returns:
            Dictionary containing path information
        """
        if name not in self.paths:
            raise KeyError(f"Path '{name}' not found in PathManager")

        return self.paths[name]

    def find_python_files(self, path_name: str, excluded_patterns: Optional[List[str]] = None) -> List[str]:
        """
        Find Python files in the specified path, excluding __init__.py files.

        Args:
            path_name: Name of the path to search in
            excluded_patterns: Additional patterns to exclude

        Returns:
            List of absolute file paths to Python files
        """
        path_info = self.get_path(path_name)
        path = path_info["path"]

        # Combine default and additional exclusion patterns
        all_excluded_patterns = self._excluded_patterns.copy()
        if excluded_patterns:
            all_excluded_patterns.extend(excluded_patterns)

        # Add __init__.py to excluded patterns
        all_excluded_patterns.append("__init__.py")

        # Use DirectoryPath to find Python files
        dir_path = DirectoryPath(path, all_excluded_patterns, [".py"])

        # Convert Path objects to strings for compatibility with existing code
        return [str(file_path) for file_path in dir_path.get_files_list()]

    def iterate_python_files(self, path_name: str, excluded_patterns: Optional[List[str]] = None) -> Iterator[Path]:
        """
        Iterate over Python files in the specified path, excluding __init__.py files.

        Args:
            path_name: Name of the path to search in
            excluded_patterns: Additional patterns to exclude

        Returns:
            Iterator of Path objects for Python files
        """
        path_info = self.get_path(path_name)
        path = path_info["path"]

        # Combine default and additional exclusion patterns
        all_excluded_patterns = self._excluded_patterns.copy()
        if excluded_patterns:
            all_excluded_patterns.extend(excluded_patterns)

        # Add __init__.py to excluded patterns
        all_excluded_patterns.append("__init__.py")

        # Use DirectoryPath to iterate over Python files
        dir_path = DirectoryPath(path, all_excluded_patterns, [".py"])
        return dir_path.iterate_files()

    def get_drops(self, path_name: str) -> str:
        """
        Get the drops value for a path.

        Args:
            path_name: Name of the path

        Returns:
            The drops value for the path
        """
        path_info = self.get_path(path_name)
        return path_info["drops"]

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "PathManager":
        """
        Create a PathManager from configuration.

        Args:
            config: Configuration object containing path information

        Returns:
            Initialized PathManager instance
        """
        # Get use_pickle from config

        use_pickle = config.get("parameters", {}).get("general", {}).get("use_pickle", False)

        path_manager = cls(use_pickle=use_pickle)

        config_paths = config.get("paths", {})
        for path_name, path_info in config_paths.items():
            path_manager.add_path(path_name, path_info["path"], path_info.get("drops", ""))

        return path_manager

    def find_parent_path(self, path: str) -> Optional[str]:
        """
        Find the parent path from self.paths that contains the given path.

        Args:
            path: The path to find the parent of

        Returns:
            The name of the parent path from self.paths that contains the given path,
            or None if no parent is found
        """
        path_obj = Path(path)

        # Convert to the absolute path if needed
        if not path_obj.is_absolute():
            path_obj = self.root_dir / path_obj

        # Search through all paths in self.paths to find the one that contains path_obj
        best_match = None
        best_match_depth = -1

        for path_name, path_info in self.paths.items():
            registered_path = Path(path_info["path"])

            try:
                # Check if path_obj is under this registered path
                path_obj.relative_to(registered_path)

                # Calculate depth (number of parent directories)
                depth = len(registered_path.parts)

                # Keep the deepest match (the most specific parent)
                if depth > best_match_depth:
                    best_match = path_name
                    best_match_depth = depth

            except ValueError:
                # path_obj is not under this registered path, continue
                continue

        return best_match

path_manager = PathManager()
