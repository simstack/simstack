from pathlib import Path
import pytest
from simstack.util.path_manager import PathManager
from simstack.util.project_root_finder import find_project_root

pytestmark = pytest.mark.skip(
    reason="PathManager tests disabled: class is no longer used."
)


@pytest.fixture
def path_manager_fixture():
    """Set up test fixtures."""
    path_manager = PathManager(use_pickle=False)
    root_dir = find_project_root()

    # Add test paths
    path_manager.add_path(
        "test_models",
        Path(root_dir) / "src" / "simstack" / "models",
        "src",
    )
    path_manager.add_path(
        "test_methods",
        Path(root_dir) / "src" / "simstack" / "methods",
        "src",
    )
    return path_manager, root_dir


def test_add_path(path_manager_fixture):
    """Test adding a path to the PathManager."""
    path_manager, root_dir = path_manager_fixture
    # Add a new path
    test_path = Path(root_dir) / "tests"
    path_manager.add_path("test_tests", test_path, "")

    # Verify the path was added
    path_info = path_manager.get_path("test_tests")
    assert path_info["path"] == test_path
    assert path_info["drops"] == ""


def test_get_path(path_manager_fixture):
    """Test getting a path from the PathManager."""
    path_manager, root_dir = path_manager_fixture
    # Get an existing path
    path_info = path_manager.get_path("test_models")
    expected_path = root_dir / "src" / "simstack" / "models"
    assert path_info["path"] == expected_path
    assert path_info["drops"] == "src"

    # Test getting a non-existent path
    with pytest.raises(KeyError):
        path_manager.get_path("non_existent_path")


def test_find_python_files(path_manager_fixture):
    """Test finding Python files in a path."""
    path_manager, _ = path_manager_fixture
    # Find Python files in the models directory
    python_files = path_manager.find_python_files("test_models")

    # Verify that files were found
    assert len(python_files) > 0

    # Verify that all files are Python files
    for file_path in python_files:
        assert file_path.endswith(".py")

    # Verify that __init__.py files are excluded
    for file_path in python_files:
        assert Path(file_path).name != "__init__.py"


def test_iterate_python_files(path_manager_fixture):
    """Test iterating over Python files in a path."""
    path_manager, _ = path_manager_fixture
    # Iterate over Python files in the models directory
    python_files = list(path_manager.iterate_python_files("test_models"))

    # Verify that files were found
    assert len(python_files) > 0

    # Verify that all files are Python files
    for file_path in python_files:
        assert str(file_path).endswith(".py")

    # Verify that __init__.py files are excluded
    for file_path in python_files:
        assert Path(file_path).name != "__init__.py"


def test_get_drops(path_manager_fixture):
    """Test getting the drops value for a path."""
    path_manager, _ = path_manager_fixture
    # Get drops for an existing path
    drops = path_manager.get_drops("test_models")
    assert drops == "src"

    # Test getting drops for a non-existent path
    with pytest.raises(KeyError):
        path_manager.get_drops("non_existent_path")


@pytest.mark.skip(reason="pathmanager is a singleton, from_config makes no sense ")
def test_from_config(path_manager_fixture):
    """Test creating a PathManager from configuration."""
    _, root_dir = path_manager_fixture
    # Create a mock configuration
    config = {"parameters": {}}
    config["parameters"]["general"] = {"use_pickle": True}

    path_manager = PathManager.from_config(config)

    # Verify the PathManager was created correctly
    assert path_manager.use_pickle

    # Verify the paths were added correctly
    path_info = path_manager.get_path("config_models")
    expected_path = Path(root_dir) / "src" / "simstack" / "models"
    assert path_info["path"] == expected_path
    assert path_info["drops"] == "src"
