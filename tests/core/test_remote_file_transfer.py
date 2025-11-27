import os
import logging
import tempfile
import time
from pathlib import Path
import zlib
import shutil

import pytest
from unittest.mock import patch

from src.simstack.models.files import FileStack
from simstack.models.file_instance import FileInstance

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Sample file content for testing
SAMPLE_CONTENT = b"This is a test file content for remote transfer test."
DUMMY_HASH = "dummy_hash_for_testing"  # Dummy hash for testing


class MockContext:
    """Mock context for simulating the simstack context during tests."""

    def __init__(self, workdir, resource_name="local"):
        class Config:
            def __init__(self, workdir, resource_name):
                self.workdir = workdir
                self.resource = resource_name
                # Simplified route configuration for testing
                self.routes = [
                    # Format: (source, target, executing_host)
                    ("local", "remote1", "local"),
                    ("remote1", "local", "remote1"),
                    ("local", "remote2", "local"),
                    ("remote2", "local", "remote2"),
                    ("remote1", "remote2", "remote1"),
                    ("remote2", "remote1", "remote2"),
                ]

        self.config = Config(workdir, resource_name)


@pytest.fixture
def setup_test_env():
    """Set up temporary directories and files for testing."""
    # Create a temporary directory tree
    base_dir = tempfile.mkdtemp(prefix="filestack_test_")
    workdir = Path(base_dir) / "workdir"
    output_dir = Path(base_dir) / "output"

    # Create test file structure
    workdir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    # Create a test file
    test_file_path = workdir / "source_file.txt"
    with open(test_file_path, "wb") as f:
        f.write(SAMPLE_CONTENT)

    # Return the created paths
    try:
        yield {
            "base_dir": base_dir,
            "workdir": workdir,
            "output_dir": output_dir,
            "test_file_path": test_file_path,
        }
    finally:
        # Clean up: Remove the temporary directory
        shutil.rmtree(base_dir)


# Patch the module's context
@pytest.fixture
def mock_context(setup_test_env):
    """Mock the simstack context for testing."""
    with patch(
        "src.simstack.models.files.context", MockContext(setup_test_env["workdir"])
    ):
        yield


@pytest.fixture
def mock_job_submission():
    """Mock the job submission and status check functions."""
    with patch("src.simstack.models.files.submit_copy_job") as mock_submit, patch(
        "src.simstack.models.files.check_job_status"
    ) as mock_status:
        # Configure the mock job status to simulate completion after a few checks
        mock_status.side_effect = ["PENDING", "RUNNING", "COMPLETED"]

        # Let the submit function return a predictable job ID
        mock_submit.return_value = "test_job_id_12345"

        yield mock_submit, mock_status


@pytest.fixture
def mock_file_creation(setup_test_env, mock_context):
    """Create a file in the target location to simulate successful transfer."""

    def _create_file(file_path):
        # Simulate the file being created by the remote copy process
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(SAMPLE_CONTENT)
        return file_path

    return _create_file


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_filestack_get_local_in_memory(setup_test_env, mock_context):
    """Test retrieving a file that exists locally in memory."""
    # Arrange
    stack_ref = "test_stack_ref"

    # Create a FileInstance (in-memory) on the local resource
    instance = FileInstance(
        path=f"testuser/{stack_ref}/test_file.txt",
        resource="local",  # Same as context.config.resource
        in_memory=True,
        content=zlib.compress(SAMPLE_CONTENT),
        is_hashable=True,
        hash=DUMMY_HASH,  # Add hash field
        created_at=time.time(),
        file_stack_id=stack_ref,
    )

    # Create a FileStack with this instance
    file_stack = FileStack(stack_ref=stack_ref, locations=[instance])

    # Act
    output_path = file_stack.get(mock_context.config.resource, local_dir=setup_test_env["output_dir"])

    # Assert
    assert output_path.exists(), "Output file should exist"
    with open(output_path, "rb") as f:
        content = f.read()
    assert content == SAMPLE_CONTENT, "Content should match the original"
    assert output_path.name == "test_file.txt", "Filename should be preserved"


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_filestack_get_local_on_disk(setup_test_env, mock_context):
    """Test retrieving a file that exists locally on disk."""
    # Arrange
    stack_ref = "test_stack_ref"

    # Create source file in workdir
    rel_path = "testuser/test_stack_ref/test_file.txt"
    source_path = setup_test_env["workdir"] / rel_path
    os.makedirs(os.path.dirname(source_path), exist_ok=True)
    with open(source_path, "wb") as f:
        f.write(SAMPLE_CONTENT)

    # Create a FileInstance (on-disk) on the local resource
    instance = FileInstance(
        path=rel_path,
        resource="local",  # Same as context.config.resource
        in_memory=False,
        content=None,
        is_hashable=True,
        hash=DUMMY_HASH,  # Add hash field
        created_at=time.time(),
        file_stack_id=stack_ref,
    )

    # Create a FileStack with this instance
    file_stack = FileStack(stack_ref=stack_ref, locations=[instance])

    # Act
    output_path = file_stack.get(None, local_dir=setup_test_env["output_dir"])

    # Assert
    assert output_path.exists(), "Output file should exist"
    with open(output_path, "rb") as f:
        content = f.read()
    assert content == SAMPLE_CONTENT, "Content should match the original"
    assert output_path.name == "test_file.txt", "Filename should be preserved"


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_filestack_get_remote(
    setup_test_env, mock_context, mock_job_submission, mock_file_creation
):
    """Test retrieving a file from a remote resource."""
    # Arrange
    stack_ref = "test_stack_ref"
    mock_submit, mock_status = mock_job_submission

    # Create a FileInstance on a remote resource
    instance = FileInstance(
        path="testuser/test_stack_ref/remote_file.txt",
        resource="remote1",  # Different from context.config.resource
        in_memory=False,
        content=None,
        is_hashable=True,
        hash=DUMMY_HASH,  # Add hash field
        created_at=time.time(),
        file_stack_id=stack_ref,
    )

    # Create a FileStack with this remote instance
    file_stack = FileStack(stack_ref=stack_ref, locations=[instance])

    # Mock the minimal_route_finder function
    with patch("src.simstack.models.files.find_shortest_route") as mock_route_finder:
        # Set up route finder to return a valid route
        mock_route_finder.return_value = [
            ("remote1", "local", "remote1")  # Format: (source, target, executing_host)
        ]

        # Simulate file being created after job submission (this would happen via the remote copy process)
        def create_file_after_status(*args, **kwargs):
            if (
                mock_status.call_count == 2
            ):  # After the second status check (when "RUNNING")
                expected_output_path = setup_test_env["output_dir"] / "remote_file.txt"
                mock_file_creation(expected_output_path)
            return (
                mock_status.side_effect.pop(0)
                if mock_status.side_effect
                else "COMPLETED"
            )

        mock_status.side_effect = create_file_after_status

        # Act
        output_path = file_stack.get(None, local_dir=setup_test_env["output_dir"])

        # Assert
        assert output_path.exists(), "Output file should exist"
        with open(output_path, "rb") as f:
            content = f.read()
        assert content == SAMPLE_CONTENT, "Content should match the expected"
        assert output_path.name == "remote_file.txt", "Filename should be preserved"

        # Verify the correct functions were called
        mock_route_finder.assert_called_once()
        mock_submit.assert_called_once_with(
            source_node="remote1",
            source_path="testuser/test_stack_ref/remote_file.txt",
            target_node="local",
            target_path=str(setup_test_env["output_dir"] / "remote_file.txt"),
            executing_node="remote1",
        )
        assert mock_status.call_count > 0, "Job status should have been checked"


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_filestack_get_multi_remote_first_fails(
    setup_test_env, mock_context, mock_job_submission, mock_file_creation
):
    """Test retrieving a file when multiple remote instances exist but the first transfer fails."""
    # Arrange
    stack_ref = "test_stack_ref"
    mock_submit, mock_status = mock_job_submission

    # Create two FileInstances on different remote resources
    instance1 = FileInstance(
        path="testuser/test_stack_ref/same_name.txt",  # Use the same filename for both instances
        resource="remote1",
        in_memory=False,
        content=None,
        is_hashable=True,
        hash=DUMMY_HASH,  # Add hash field
        created_at=time.time(),
        file_stack_id=stack_ref,
    )

    instance2 = FileInstance(
        path="testuser/test_stack_ref/same_name.txt",  # Use the same filename for both instances
        resource="remote2",
        in_memory=False,
        content=None,
        is_hashable=True,
        hash=DUMMY_HASH,  # Add hash field
        created_at=time.time(),
        file_stack_id=stack_ref,
    )

    # Create a FileStack with both remote instances
    file_stack = FileStack(stack_ref=stack_ref, locations=[instance1, instance2])

    # Mock the minimal_route_finder function
    with patch("src.simstack.models.files.find_shortest_route") as mock_route_finder:
        # Set up route finder to return valid routes for both resources
        def route_finder_side_effect(source, target, routes):
            if source == "remote1":
                return [("remote1", "local", "remote1")]
            elif source == "remote2":
                return [("remote2", "local", "remote2")]
            return None

        mock_route_finder.side_effect = route_finder_side_effect

        # Configure job status checks to fail for the first instance, succeed for the second
        first_job_checks = ["RUNNING", "FAILED"]
        second_job_checks = ["RUNNING", "COMPLETED"]

        # Expected path where FileStack.get() will look for the file
        expected_file_path = setup_test_env["output_dir"] / "same_name.txt"

        # Track which job is being processed
        current_job_source = None

        def status_check_side_effect(job_id):
            nonlocal current_job_source

            if "remote1" in job_id:
                current_job_source = "remote1"
                # First job fails - don't create file
                return first_job_checks.pop(0) if first_job_checks else "FAILED"
            else:
                current_job_source = "remote2"
                # Second job succeeds and creates the file
                result = second_job_checks.pop(0) if second_job_checks else "COMPLETED"
                if result == "COMPLETED":
                    # Create the file at the exact path that FileStack.get() is expecting
                    mock_file_creation(expected_file_path)
                return result

        # Configure submit to return different job IDs
        submit_call_count = 0

        def submit_side_effect(*args, **kwargs):
            nonlocal submit_call_count
            submit_call_count += 1
            source_node = args[0] if args else kwargs.get("source_node")
            target_path = args[3] if len(args) > 3 else kwargs.get("target_path")

            # Store the target path for debugging
            print(
                f"Mock submit called with source: {source_node}, target_path: {target_path}"
            )

            return f"test_job_{source_node}_{submit_call_count}"

        mock_submit.side_effect = submit_side_effect
        mock_status.side_effect = status_check_side_effect

        # Act
        output_path = file_stack.get(None, local_dir=setup_test_env["output_dir"])

        # Assert
        assert output_path.exists(), "Output file should exist"
        assert output_path.name == "same_name.txt", "Should have the expected filename"

        # Verify that both remote instances were tried
        assert (
            mock_submit.call_count == 2
        ), "Both remote instances should have been tried"
        assert (
            "remote1" in mock_submit.call_args_list[0][1]["source_node"]
        ), "First attempt should be remote1"
        assert (
            "remote2" in mock_submit.call_args_list[1][1]["source_node"]
        ), "Second attempt should be remote2"


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_filestack_get_no_valid_instances(
    setup_test_env, mock_context, mock_job_submission
):
    """Test case where there are no valid instances or routes available."""
    # Arrange
    stack_ref = "test_stack_ref"
    mock_submit, mock_status = mock_job_submission

    # Create a FileInstance on a remote resource with no route
    instance = FileInstance(
        path="testuser/test_stack_ref/unreachable_file.txt",
        resource="unreachable",  # A resource with no route to local
        in_memory=False,
        content=None,
        is_hashable=True,
        hash=DUMMY_HASH,  # Add hash field
        created_at=time.time(),
        file_stack_id=stack_ref,
    )

    # Create a FileStack with this unreachable instance
    file_stack = FileStack(stack_ref=stack_ref, locations=[instance])

    # Mock the minimal_route_finder to return no route
    with patch("src.simstack.models.files.find_shortest_route") as mock_route_finder:
        mock_route_finder.return_value = None  # No route available

        # Act and Assert
        with pytest.raises(ValueError) as excinfo:
            file_stack.get(None, local_dir=setup_test_env["output_dir"])

        # Check that the error message is appropriate
        assert "No suitable file instance found locally" in str(excinfo.value)
        assert "no available transfer route succeeded" in str(excinfo.value)

        # Verify the route finder was called but submit_job was not
        mock_route_finder.assert_called_once()
        mock_submit.assert_not_called()


# Run the tests if this file is executed directly
if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
