import os
import socket
from pathlib import Path
import pytest
from simstack.models.resource_definition import ResourceDefinition, GitRepo

class TestResourceDefinition:
    @pytest.fixture
    def valid_paths(self, tmp_path):
        """Create valid temporary paths for testing."""
        ssh_key = tmp_path / "id_rsa"
        ssh_key.touch()

        python_path = tmp_path / "python_lib"
        python_path.mkdir()

        return {
            "ssh_key": ssh_key,
            "python_path": python_path,
            "workdir": tmp_path / "work"
        }

    def test_valid_resource_definition(self, valid_paths):
        """Test creating a valid ResourceDefinition instance."""

        resource = ResourceDefinition(
            resource_str="local",
            ssh_key=valid_paths["ssh_key"],
            workdir=valid_paths["workdir"],
            hostname=socket.gethostname(),
            python_paths=[valid_paths["python_path"]],
            environment_start="conda activate env",

        )

        assert resource.resource_str == "local"
        assert resource.ssh_key == valid_paths["ssh_key"]
        assert len(resource.python_paths) == 1


    def test_optional_ssh_key(self, valid_paths):
        """Test that ssh_key is optional."""


        resource = ResourceDefinition(
            resource_str="local",
            ssh_key=None,
            hostname = socket.gethostname(),
            workdir=valid_paths["workdir"],
            python_paths=[valid_paths["python_path"]],
            environment_start="cmd",

        )
        assert resource.ssh_key is None

    def test_invalid_ssh_key(self, valid_paths):
        """Test validation of a non-existent SSH key path."""

        with pytest.raises(FileNotFoundError):
            resource_definition = ResourceDefinition(
                resource_str="local",
                ssh_key="/path/to/nowhere/key",
                workdir=valid_paths["workdir"],
                hostname=socket.gethostname(),
                python_paths=[valid_paths["python_path"]],
                environment_start="cmd",

            )
            resource_definition.validate_ssh_key()

    def test_invalid_python_path(self, valid_paths):
        """Test validation of non-existent python path."""

        with pytest.raises(FileNotFoundError):
            resource_definition = ResourceDefinition(
                resource_str="local",
                ssh_key=None,
                workdir=valid_paths["workdir"],
                hostname=socket.gethostname(),
                python_paths=["/path/to/nowhere/lib"],
                environment_start="cmd",

            )
            resource_definition.validate_python_path()

    def test_assignment_validation(self, valid_paths):
        """Test that validation runs on assignment."""

        resource = ResourceDefinition(
            resource_str="local",
            ssh_key=valid_paths["ssh_key"],
            workdir=valid_paths["workdir"],
            hostname=socket.gethostname(),
            python_paths=[valid_paths["python_path"]],
            environment_start="cmd",
        )

        # Test valid assignment
        resource.ssh_key = valid_paths["ssh_key"]
