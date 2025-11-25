from pathlib import Path
from typing import Optional


class ConfigFile:
    """
    Singleton class that manages the path to a configuration file.

    This class ensures only one instance exists throughout the application
    and provides a centralized way to access the configuration file path.
    """

    _instance: Optional["ConfigFile"] = None
    _config_file_path: Optional[str] = "simstack.toml"  # Default config file name

    def __new__(cls) -> "ConfigFile":
        """Create or return the singleton instance."""
        if cls._instance is None:
            cls._instance = super(ConfigFile, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the ConfigManager (only runs once due to singleton pattern)."""
        # Only initialize once
        if not hasattr(self, "_initialized"):
            self._initialized = True

    @property
    def config_file_path(self) -> Optional[str]:
        """Get the current configuration file path."""
        return self._config_file_path

    @config_file_path.setter
    def config_file_path(self, path: str) -> None:
        """
        Set the configuration file path.

        Args:
            path: Path to the configuration file

        Raises:
            ValueError: If the path is empty or None
            FileNotFoundError: If the file doesn't exist (optional validation)
        """
        if not path:
            raise ValueError("Configuration file path cannot be empty or None")

        # Convert to absolute path for consistency
        self._config_file_path = str(Path(path).resolve())


# Convenience function to get the singleton instance
def get_config_file() -> ConfigFile:
    """
    Get the singleton ConfigManager instance.

    Returns:
        The ConfigManager singleton instance
    """
    return ConfigFile()
