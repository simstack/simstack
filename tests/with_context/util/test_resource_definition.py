import os
from pathlib import Path
import pytest
from simstack.models.resource_definition import ResourceDefinition, GitRepo

class TestGitRepo:
    def test_valid_git_repo(self):
        """Test creating a valid GitRepo instance."""
        repo = GitRepo(url="https://github.com/user/repo.git", branch="main")
        assert repo.url == "https://github.com/user/repo.git"
        assert repo.branch == "main"
        assert repo.is_submodule is False

    def test_git_repo_defaults(self):
        """Test GitRepo defaults."""
        repo = GitRepo(url="https://github.com/user/repo.git", branch="main")
        assert repo.is_submodule is False

    def test_invalid_git_url(self):
        """Test validation of invalid git URLs."""
        with pytest.raises(ValueError, match="Invalid URL format"):
            GitRepo(url="not-a-url", branch="main")

        with pytest.raises(ValueError, match="Invalid URL format"):
            GitRepo(url="", branch="main")

class TestResourceDefinition:
    @pytest.fixture
    def valid_paths(self, tmp_path):
        """Create valid temporary paths for testing."""
        ssh_key = tmp_path / "id_rsa"
        ssh_key.touch()

        python_path = tmp_path / "python_lib"
        python_path.mkdir()

        return {
            "ssh_key": str(ssh_key),
            "python_path": str(python_path),
            "workdir": str(tmp_path / "work")
        }

    def test_valid_resource_definition(self, valid_paths):
        """Test creating a valid ResourceDefinition instance."""
        git_repo = GitRepo(url="https://github.com/user/repo.git", branch="main")

        resource = ResourceDefinition(
            name="local-resource",
            ssh_key_path=valid_paths["ssh_key"],
            workdir=valid_paths["workdir"],
            python_path=[valid_paths["python_path"]],
            environment_start="conda activate env",
            git=[git_repo]
        )

        assert resource.name == "local-resource"
        assert resource.ssh_key == valid_paths["ssh_key"]
        assert len(resource.python_paths) == 1
        assert len(resource.git) == 1

    def test_optional_ssh_key(self, valid_paths):
        """Test that ssh_key_path is optional."""
        git_repo = GitRepo(url="https://github.com/user/repo.git", branch="main")

        resource = ResourceDefinition(
            name="local-resource",
            ssh_key_path=None,
            workdir=valid_paths["workdir"],
            python_path=[valid_paths["python_path"]],
            environment_start="cmd",
            git=[git_repo]
        )
        assert resource.ssh_key is None

    def test_invalid_ssh_key_path(self, valid_paths):
        """Test validation of a non-existent SSH key path."""
        git_repo = GitRepo(url="https://github.com/user/repo.git", branch="main")

        with pytest.raises(ValueError, match="SSH key path does not exist"):
            ResourceDefinition(
                name="local-resource",
                ssh_key_path="/path/to/nowhere/key",
                workdir=valid_paths["workdir"],
                python_path=[valid_paths["python_path"]],
                environment_start="cmd",
                git=[git_repo]
            )

    def test_invalid_python_path(self, valid_paths):
        """Test validation of non-existent python path."""
        git_repo = GitRepo(url="https://github.com/user/repo.git", branch="main")

        with pytest.raises(ValueError, match="Python path does not exist"):
            ResourceDefinition(
                name="local-resource",
                ssh_key_path=None,
                workdir=valid_paths["workdir"],
                python_path=["/path/to/nowhere/lib"],
                environment_start="cmd",
                git=[git_repo]
            )

    def test_assignment_validation(self, valid_paths):
        """Test that validation runs on assignment."""
        git_repo = GitRepo(url="https://github.com/user/repo.git", branch="main")

        resource = ResourceDefinition(
            name="local-resource",
            ssh_key_path=valid_paths["ssh_key"],
            workdir=valid_paths["workdir"],
            python_path=[valid_paths["python_path"]],
            environment_start="cmd",
            git=[git_repo]
        )

        # Test valid assignment
        resource.ssh_key = valid_paths["ssh_key"]

        # Test invalid assignment
        with pytest.raises(ValueError, match="SSH key path does not exist"):
            resource.ssh_key = "/non/existent/path"

        with pytest.raises(ValueError, match="Python path does not exist"):
            resource.python_paths = ["/non/existent/path"]