import os
from pathlib import Path
from typing import List, Dict, Optional, Iterator, Any

from simstack.util.directory_iterator import DirectoryPath
from simstack.util.project_root_finder import find_project_root


class PathManager:
    """
    Manages paths for the SimStack application, providing mechanisms to find Python files
    and other resources. Uses DirectoryPath for efficient directory traversal.
    """

    def __init__(self, use_pickle: bool = False):
        """
        Initialize the PathManager.

        Args:
            use_pickle: Boolean flag indicating whether to use pickle for serialization
        """
        self.use_pickle = use_pickle
        self.root_dir = find_project_root()
        self.paths: Dict[str, Dict[str, str]] = {}
        self.packages: Dict[str] = {}
        self.precompiled: Dict[str] = {}
        self._default_excluded_patterns = [
            "__pycache__",
            "*.pyc",
            ".git",
            ".venv",
            "venv",
        ]

    def add_path(
        self, name: str, path: str, drops: str = "", use_pickle: bool = False
    ) -> None:
        """
        Add a path to the manager.

        Args:
            name: Name identifier for the path
            path: The directory path
            drops: Prefix to drop from module names (for import paths)
            use_pickle: Whether to use pickle for this path
        """
        if not os.path.isdir(path):
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

    def find_python_files(
        self, path_name: str, excluded_patterns: Optional[List[str]] = None
    ) -> List[str]:
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
        all_excluded_patterns = self._default_excluded_patterns.copy()
        if excluded_patterns:
            all_excluded_patterns.extend(excluded_patterns)

        # Add __init__.py to excluded patterns
        all_excluded_patterns.append("__init__.py")

        # Use DirectoryPath to find Python files
        dir_path = DirectoryPath(path, all_excluded_patterns, [".py"])

        # Convert Path objects to strings for compatibility with existing code
        return [str(file_path) for file_path in dir_path.get_files_list()]

    def iterate_python_files(
        self, path_name: str, excluded_patterns: Optional[List[str]] = None
    ) -> Iterator[Path]:
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
        all_excluded_patterns = self._default_excluded_patterns.copy()
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
    def from_config(cls, config: Any) -> "PathManager":
        """
        Create a PathManager from configuration.

        Args:
            config: Configuration object containing path information

        Returns:
            Initialized PathManager instance
        """
        # Get use_pickle from config
        use_pickle = False
        if hasattr(config, "config"):
            # If config is a ConfigReader instance
            use_pickle = (
                config.config.get("parameters", {})
                .get("common", {})
                .get("use_pickle", False)
            )
        else:
            # If config is a dictionary
            use_pickle = config.get("use_pickle", False)

        path_manager = cls(use_pickle=use_pickle)

        # Get paths from config
        if hasattr(config, "paths"):
            # If config is a ConfigReader instance
            paths = config.paths
        else:
            # If config is a dictionary
            paths = config.get("paths", {})

        # Add paths from configuration if available
        root_dir = find_project_root()
        for name, path_info in paths.items():
            if isinstance(path_info, dict) and "path" in path_info:
                # Get path, drops, and use_pickle from path_info
                path = path_info["path"]
                # Convert Unix-style path to Windows path if on Windows
                if os.name == "nt":  # Windows OS
                    path = path.replace("/", "\\")

                drops = path_info.get("drops", "")
                path_use_pickle = path_info.get("use_pickle", False)

                # Convert relative paths to absolute paths
                if not os.path.isabs(path):
                    path = os.path.join(root_dir, path)

                # Add path to PathManager
                path_manager.add_path(name, path, drops, path_use_pickle)

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
