from pathlib import Path

import pytest
from simstack.toml_reader import TomlReader


class TestTomlReader:
    """Test suite for the TomlReader class."""

    def test_initialize_with_default_path(self, mocker):
        """Test initialization with the default path and file."""
        mocked_find_project_root = mocker.patch(
            "simstack.toml_reader.find_project_root", return_value=Path("/mock/root")
        )
        mocked_tomllib_load = mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key": "value"})
        mocked_open = mocker.patch("builtins.open", mocker.mock_open(read_data="mock data"))

        reader = TomlReader()

        mocked_find_project_root.assert_called_once()
        mocked_tomllib_load.assert_called_once()
        mocked_open.assert_called_once_with(Path("/mock/root/simstack.toml"), "rb")
        assert reader.config == {"key": "value"}

    def test_initialize_with_custom_path(self, mocker):
        """Test initialization with a custom path and file."""
        mocked_tomllib_load = mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key": "value"})
        mocked_open = mocker.patch("builtins.open", mocker.mock_open(read_data="mock data"))

        custom_path = Path("/custom/path")
        custom_file = "custom.toml"
        reader = TomlReader(config_path=custom_path, config_file=custom_file)

        mocked_open.assert_called_once_with(custom_path / custom_file, "rb")
        mocked_tomllib_load.assert_called_once()
        assert reader.config == {"key": "value"}

    def test_initialize_file_not_found(self, mocker):
        """Test behavior when the TOML file is not found."""
        mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))

        with pytest.raises(SystemExit) as excinfo:
            TomlReader(config_file="non_existent.toml")
        assert excinfo.value.code == -1

    def test_initialize_invalid_toml(self, mocker):
        """Test behavior when the TOML file contains invalid syntax."""
        mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
        mocker.patch("builtins.open", mocker.mock_open(read_data="invalid toml"))
        mocker.patch("simstack.toml_reader.tomllib.load", side_effect=tomllib.TOMLDecodeError("error", "doc", 1))

        with pytest.raises(SystemExit) as excinfo:
            TomlReader()
        assert excinfo.value.code == -1

    def test_get_existing_key(self, mocker):
        """Test retrieving an existing key."""
        mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
        mocker.patch("builtins.open", mocker.mock_open(read_data="mock"))
        mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key1": {"key2": "value"}})

        reader = TomlReader()
        assert reader.get("key1.key2") == "value"

    def test_get_non_existing_key(self, mocker):
        """Test retrieving a non-existing key."""
        mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
        mocker.patch("builtins.open", mocker.mock_open(read_data="mock"))
        mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key1": {"key2": "value"}})

        reader = TomlReader()
        assert reader.get("key1.key3") is None

    def test_get_with_default_value(self, mocker):
        """Test retrieving a non-existing key with a default value."""
        mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
        mocker.patch("builtins.open", mocker.mock_open(read_data="mock"))
        mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key1": {"key2": "value"}})

        reader = TomlReader()
        assert reader.get("key1.key3", default="default_value") == "default_value"
