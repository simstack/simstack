import os
import unittest
from simstack.util.path_manager import PathManager
from simstack.util.project_root_finder import find_project_root


class TestPathManager(unittest.TestCase):
    """Test cases for the PathManager class."""

    def setUp(self):
        """Set up test fixtures."""
        self.path_manager = PathManager(use_pickle=False)
        self.root_dir = find_project_root()

        # Add test paths
        self.path_manager.add_path(
            "test_models",
            os.path.join(self.root_dir, "src", "simstack", "models"),
            "src",
        )
        self.path_manager.add_path(
            "test_methods",
            os.path.join(self.root_dir, "src", "simstack", "methods"),
            "src",
        )

    def test_add_path(self):
        """Test adding a path to the PathManager."""
        # Add a new path
        test_path = os.path.join(self.root_dir, "tests")
        self.path_manager.add_path("test_tests", test_path, "")

        # Verify the path was added
        path_info = self.path_manager.get_path("test_tests")
        self.assertEqual(path_info["path"], test_path)
        self.assertEqual(path_info["drops"], "")

    def test_get_path(self):
        """Test getting a path from the PathManager."""
        # Get an existing path
        path_info = self.path_manager.get_path("test_models")
        expected_path = os.path.join(self.root_dir, "src", "simstack", "models")
        self.assertEqual(path_info["path"], expected_path)
        self.assertEqual(path_info["drops"], "src")

        # Test getting a non-existent path
        with self.assertRaises(KeyError):
            self.path_manager.get_path("non_existent_path")

    def test_find_python_files(self):
        """Test finding Python files in a path."""
        # Find Python files in the models directory
        python_files = self.path_manager.find_python_files("test_models")

        # Verify that files were found
        self.assertGreater(len(python_files), 0)

        # Verify that all files are Python files
        for file_path in python_files:
            self.assertTrue(file_path.endswith(".py"))

        # Verify that __init__.py files are excluded
        for file_path in python_files:
            self.assertFalse(os.path.basename(file_path) == "__init__.py")

    def test_iterate_python_files(self):
        """Test iterating over Python files in a path."""
        # Iterate over Python files in the models directory
        python_files = list(self.path_manager.iterate_python_files("test_models"))

        # Verify that files were found
        self.assertGreater(len(python_files), 0)

        # Verify that all files are Python files
        for file_path in python_files:
            self.assertTrue(str(file_path).endswith(".py"))

        # Verify that __init__.py files are excluded
        for file_path in python_files:
            self.assertFalse(os.path.basename(str(file_path)) == "__init__.py")

    def test_get_drops(self):
        """Test getting the drops value for a path."""
        # Get drops for an existing path
        drops = self.path_manager.get_drops("test_models")
        self.assertEqual(drops, "src")

        # Test getting drops for a non-existent path
        with self.assertRaises(KeyError):
            self.path_manager.get_drops("non_existent_path")

    def test_from_config(self):
        """Test creating a PathManager from configuration."""
        # Create a mock configuration
        root_dir = self.root_dir  # Get root_dir from the test class

        class MockConfig:
            def get(self, key, default=None):
                if key == "use_pickle":
                    return True
                elif key == "paths":
                    return {
                        "config_models": {
                            "path": os.path.join(root_dir, "src", "simstack", "models"),
                            "drops": "src",
                        }
                    }
                return default

        # Create a PathManager from the mock configuration
        config = MockConfig()
        path_manager = PathManager.from_config(config)

        # Verify the PathManager was created correctly
        self.assertTrue(path_manager.use_pickle)

        # Verify the paths were added correctly
        path_info = path_manager.get_path("config_models")
        expected_path = os.path.join(self.root_dir, "src", "simstack", "models")
        self.assertEqual(path_info["path"], expected_path)
        self.assertEqual(path_info["drops"], "src")


if __name__ == "__main__":
    unittest.main()
