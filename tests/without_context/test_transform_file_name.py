import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from simstack.util.project_root_finder import find_project_root
from src.simstack.util.transform_file_name import transform_file_name

@pytest.fixture
def prepared_directory_path(request):

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        (temp_path / "home").mkdir()
        (temp_path / "temp").mkdir()
        os.environ["HOME"] = str(temp_path / "home")
        os.environ["TEMP"] = str(temp_path / "temp")
        os.environ["PROJECT"] = str(find_project_root())

        yield temp_path

    # def cleanup():
    #     for key in ("HOME", "TEMP"):
    #         if key in os.environ:
    #             del os.environ[key]
    #     temp_dir.cleanup()
    #
    # request.addfinalizer(cleanup)
    # return temp_path


def test_transform_file_name_resolves_home_directory(monkeypatch, prepared_directory_path: Path):
    """Test that $HOME in the supplied path is replaced with the user's home directory."""
    temp_path = prepared_directory_path
    assert temp_path.exists()
    assert (temp_path / "home").exists()

    touch_file = temp_path / "home" / "test_file.txt"
    touch_file.touch()
    mock_path = "$HOME/test_file.txt"

    result = transform_file_name(mock_path)
    assert result == touch_file


def test_transform_file_name_resolves_temp_directory(monkeypatch, prepared_directory_path: Path):
    """Test that $TEMP in the supplied path is replaced with the appropriate temp directory."""
    temp_path = prepared_directory_path
    touch_file = temp_path / "temp" / "test_file.txt"
    touch_file.touch()
    mock_path = "$TEMP/test_file.txt"

    result = transform_file_name(mock_path)
    assert result == touch_file


def test_transform_file_name_resolves_project_directory(monkeypatch, prepared_directory_path: Path):
    """Test that $PROJECT in the supplied path is replaced with the project's root directory."""
    project_dir = os.environ["PROJECT"]
    touch_file = Path(project_dir) / "test_file.txt"
    touch_file.touch()
    mock_path = "$PROJECT/test_file.txt"

    result = transform_file_name(mock_path)
    assert result == touch_file


def test_transform_file_name_raises_error_for_nonexistent_path(monkeypatch, prepared_directory_path: Path):
    """Test that FileNotFoundError is raised if the resolved path does not exist."""
    path_does_not_exist = "$TEMP/non_existent_file.txt"

    with pytest.raises(FileNotFoundError, match="Path does not exist:"):
        transform_file_name(path_does_not_exist)

def test_path_with_var(prepared_directory_path: Path):
    temp_path = prepared_directory_path
    touch_file = temp_path / "home" / "test_file2.txt"
    touch_file.touch()
    test_path = Path("$HOME/test_file2.txt")
    full_path = transform_file_name(test_path)
    assert full_path == touch_file
