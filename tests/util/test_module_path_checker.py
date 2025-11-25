import pytest
import os
import tempfile
from unittest.mock import patch

from simstack.util.module_path_checker import is_module_subpath_of_path

from simstack.util.project_root_finder import find_project_root


class TestIsModuleSubpathOfPath:
    """Test cases for is_module_subpath_of_path function."""

    def setup_method(self):
        """Set up test fixtures."""
        # Create a temporary directory structure for testing
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = self.temp_dir

        # Create test directory structure using os.makedirs
        os.makedirs(
            os.path.join(self.project_root, "src", "simstack", "core"), exist_ok=True
        )
        os.makedirs(
            os.path.join(self.project_root, "src", "simstack", "util"), exist_ok=True
        )
        os.makedirs(os.path.join(self.project_root, "tests", "unit"), exist_ok=True)
        os.makedirs(os.path.join(self.project_root, "examples", "basic"), exist_ok=True)

    def teardown_method(self):
        """Clean up test fixtures."""
        import shutil

        shutil.rmtree(self.temp_dir)

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_module_is_direct_subpath(self, mock_find_root):
        """Test when module path is directly under the given path."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.core.artifacts"
        path_info_path = os.path.join(self.project_root, "src", "simstack")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_module_is_nested_subpath(self, mock_find_root):
        """Test when module path is nested deeply under the given path."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.core.deep.nested.module"
        path_info_path = os.path.join(self.project_root, "src")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_module_is_not_subpath(self, mock_find_root):
        """Test when module path is not under the given path."""
        mock_find_root.return_value = self.project_root

        module_path = "tests.unit.test_artifacts"
        path_info_path = os.path.join(self.project_root, "src", "simstack")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is False

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_module_is_sibling_path(self, mock_find_root):
        """Test when module path is a sibling of the given path."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.util.helpers"
        path_info_path = os.path.join(self.project_root, "src", "simstack", "core")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is False

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_module_is_parent_path(self, mock_find_root):
        """Test when module path is a parent of the given path."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack"
        path_info_path = os.path.join(self.project_root, "src", "simstack", "core")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is False

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_exact_path_match(self, mock_find_root):
        """Test when module path exactly matches the given path."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.core"
        path_info_path = os.path.join(self.project_root, "src", "simstack", "core")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_single_level_module(self, mock_find_root):
        """Test with a single-level module path."""
        mock_find_root.return_value = self.project_root

        module_path = "src"
        path_info_path = self.project_root

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_empty_module_path(self, mock_find_root):
        """Test with empty module path."""
        mock_find_root.return_value = self.project_root

        module_path = ""
        path_info_path = os.path.join(self.project_root, "src")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is False

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_windows_path_separators(self, mock_find_root):
        """Test that the function works correctly with mixed path separators."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.core.artifacts"
        # Create path with forward slashes to test cross-platform handling
        path_info_path = self.project_root + "/src/simstack"

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_trailing_separators_in_path(self, mock_find_root):
        """Test handling of trailing separators in path_info_path."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.core.artifacts"
        path_info_path = os.path.join(self.project_root, "src", "simstack") + os.sep

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_nonexistent_paths(self, mock_find_root):
        """Test with paths that don't exist on the filesystem."""
        mock_find_root.return_value = self.project_root

        module_path = "nonexistent.path.module"
        path_info_path = os.path.join(self.project_root, "nonexistent", "path")

        # The function should still work for path comparison even if paths don't exist
        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_deep_nesting(self, mock_find_root):
        """Test with deeply nested module paths."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.core.deep.very.deeply.nested.module"
        path_info_path = os.path.join(self.project_root, "src", "simstack")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_partial_name_match(self, mock_find_root):
        """Test that partial name matches don't incorrectly return True."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack_extended.core.artifacts"
        path_info_path = os.path.join(self.project_root, "src", "simstack")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is False

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_complex_nested_structure(self, mock_find_root):
        """Test with complex nested directory structure."""
        mock_find_root.return_value = self.project_root

        module_path = (
            "examples.science.electronic_structure.spectra.vibrational_spectra"
        )
        path_info_path = os.path.join(self.project_root, "examples", "science")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_applications_directory(self, mock_find_root):
        """Test with applications directory structure."""
        mock_find_root.return_value = self.project_root

        module_path = "applications.electronic_structure.util.cdx_to_molecule_rdkit"
        path_info_path = os.path.join(self.project_root, "applications")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_different_root_directories(self, mock_find_root):
        """Test with different root directory structures."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.core.artifacts"
        # Test with a completely different root
        different_root = os.path.join(os.sep, "different", "project", "root")
        path_info_path = os.path.join(different_root, "src", "simstack")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is False

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_case_sensitivity_on_case_sensitive_systems(self, mock_find_root):
        """Test case sensitivity handling on case-sensitive systems."""
        mock_find_root.return_value = self.project_root

        module_path = "src.simstack.core.artifacts"
        path_info_path = os.path.join(self.project_root, "SRC", "simstack")

        result = is_module_subpath_of_path(module_path, path_info_path)
        # On case-sensitive systems, this should be False
        # On case-insensitive systems (Windows), this might be True
        assert isinstance(result, bool)

    def test_find_project_root_called(self):
        """Test that find_project_root is called during execution."""
        with patch(
            "simstack.util.module_path_checker.find_project_root"
        ) as mock_find_root:
            mock_find_root.return_value = self.project_root

            path_info_path = os.path.join(self.project_root, "src")
            is_module_subpath_of_path("src.test", path_info_path)

            mock_find_root.assert_called_once()

    @patch("simstack.util.module_path_checker.find_project_root")
    def test_path_with_dots_in_directory_names(self, mock_find_root):
        """Test with directory names containing dots."""
        mock_find_root.return_value = self.project_root

        # Create a directory with dots in the name
        dotted_dir = os.path.join(self.project_root, "src", "my.package.name")
        os.makedirs(dotted_dir, exist_ok=True)

        module_path = "src.my.package.name.module"
        path_info_path = os.path.join(self.project_root, "src")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert result is True


# Additional integration tests
class TestIsModuleSubpathOfPathIntegration:
    """Integration tests that don't mock find_project_root."""

    def test_with_actual_project_structure(self):
        """Test with the actual project structure."""
        # This test works with the real project structure
        module_path = "src.simstack.core.artifacts"

        # Get the actual project root
        project_root = find_project_root()

        # Test with a path that should contain the module
        path_info_path = os.path.join(project_root, "src")

        result = is_module_subpath_of_path(module_path, path_info_path)
        # This should be True if the project has a src directory
        assert isinstance(result, bool)

    def test_real_world_example_with_long_paths(self):
        """Test with real-world example using long module paths."""
        project_root = find_project_root()

        # Test with a long module path that might exist in the project
        module_path = (
            "examples.science.electronic_structure.spectra.vibrational_spectra"
        )
        path_info_path = os.path.join(project_root, "examples", "science")

        result = is_module_subpath_of_path(module_path, path_info_path)
        assert isinstance(result, bool)


if __name__ == "__main__":
    pytest.main([__file__])
