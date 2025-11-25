import fnmatch
import os
from pathlib import Path
from typing import Iterator, List, Union, Optional


class DirectoryPath:
    """
    Represents a directory path with excluded subdirectory patterns and provides iteration capabilities.
    """

    def __init__(
        self,
        root_path: Union[str, Path],
        excluded_patterns: Optional[List[str]] = None,
        file_extensions: Optional[List[str]] = None,
    ):
        """
        Initialize the DirectoryPath.

        Args:
            root_path: The root directory path to traverse
            excluded_patterns: List of patterns to exclude (supports wildcards like *, ?, [])
            file_extensions: List of file extensions to include (e.g., ['.py', '.txt'])
        """
        self.root_path = Path(root_path).resolve()
        self.excluded_patterns = excluded_patterns or []
        self.file_extensions = file_extensions or []

        if not self.root_path.exists():
            raise FileNotFoundError(f"Directory does not exist: {self.root_path}")
        if not self.root_path.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {self.root_path}")

    def _is_excluded(self, path: Path) -> bool:
        """
        Check if a path matches any of the excluded patterns.

        Args:
            path: Path to check

        Returns:
            True if the path should be excluded, False otherwise
        """
        path_str = str(path)
        relative_path = str(path.relative_to(self.root_path))

        for pattern in self.excluded_patterns:
            # Check against full path
            if fnmatch.fnmatch(path_str, pattern):
                return True
            # Check against relative path
            if fnmatch.fnmatch(relative_path, pattern):
                return True
            # Check against directory name only
            if fnmatch.fnmatch(path.name, pattern):
                return True

        return False

    def _matches_extensions(self, file_path: Path) -> bool:
        """
        Check if a file matches the specified extensions.

        Args:
            file_path: File path to check

        Returns:
            True if the file matches the extensions or no extensions specified
        """
        if not self.file_extensions:
            return True

        return file_path.suffix.lower() in [ext.lower() for ext in self.file_extensions]

    def iterate_files(self) -> Iterator[Path]:
        """
        Iterate over all files in the directory tree, respecting exclusion patterns.

        Yields:
            Path objects for each file found
        """
        for root, dirs, files in os.walk(self.root_path):
            root_path = Path(root)

            # Filter out excluded directories in-place to prevent walking into them
            dirs[:] = [d for d in dirs if not self._is_excluded(root_path / d)]

            # Yield files that match criteria
            for file in files:
                file_path = root_path / file
                if not self._is_excluded(file_path) and self._matches_extensions(
                    file_path
                ):
                    yield file_path

    def iterate_directories(self) -> Iterator[Path]:
        """
        Iterate over all directories in the directory tree, respecting exclusion patterns.

        Yields:
            Path objects for each directory found
        """
        for root, dirs, _ in os.walk(self.root_path):
            root_path = Path(root)

            # Filter and yield directories
            for dir_name in dirs:
                dir_path = root_path / dir_name
                if not self._is_excluded(dir_path):
                    yield dir_path

            # Filter out excluded directories to prevent walking into them
            dirs[:] = [d for d in dirs if not self._is_excluded(root_path / d)]

    def __iter__(self) -> Iterator[Path]:
        """
        Default iterator returns files.
        """
        return self.iterate_files()

    def get_files_list(self) -> List[Path]:
        """
        Get a list of all files matching the criteria.

        Returns:
            List of Path objects
        """
        return list(self.iterate_files())

    def get_directories_list(self) -> List[Path]:
        """
        Get a list of all directories matching the criteria.

        Returns:
            List of Path objects
        """
        return list(self.iterate_directories())

    def add_excluded_pattern(self, pattern: str) -> None:
        """
        Add an exclusion pattern.

        Args:
            pattern: Pattern to exclude
        """
        if pattern not in self.excluded_patterns:
            self.excluded_patterns.append(pattern)

    def remove_excluded_pattern(self, pattern: str) -> None:
        """
        Remove an exclusion pattern.

        Args:
            pattern: Pattern to remove
        """
        if pattern in self.excluded_patterns:
            self.excluded_patterns.remove(pattern)

    def __str__(self) -> str:
        return (
            f"DirectoryPath(root='{self.root_path}', excluded={self.excluded_patterns})"
        )

    def __repr__(self) -> str:
        return (
            f"DirectoryPath(root_path='{self.root_path}', "
            f"excluded_patterns={self.excluded_patterns}, "
            f"file_extensions={self.file_extensions})"
        )


# Example usage and utility functions
def find_python_files_with_exclusions(
    root_path: str, excluded_patterns: List[str] = None
) -> List[Path]:
    """
    Find Python files using the DirectoryPath class with exclusions.

    Args:
        root_path: Root directory to search
        excluded_patterns: Patterns to exclude

    Returns:
        List of Python file paths
    """
    excluded_patterns = excluded_patterns or [
        "__pycache__",
        "*.pyc",
        ".git",
        ".venv",
        "venv",
    ]
    dir_path = DirectoryPath(root_path, excluded_patterns, [".py"])
    return dir_path.get_files_list()


if __name__ == "__main__":
    # Example usage
    try:
        # Create a DirectoryPath instance
        dp = DirectoryPath(
            root_path=".",
            excluded_patterns=["__pycache__", "*.pyc", ".git", "node_modules", "*.log"],
            file_extensions=[".py", ".txt"],
        )

        print(f"Directory Path: {dp}")
        print("\nFound files:")
        for file_path in dp:
            print(f"  {file_path}")

        print(f"\nTotal files found: {len(dp.get_files_list())}")

    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"Error: {e}")
