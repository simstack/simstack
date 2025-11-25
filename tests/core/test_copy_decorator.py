import logging
import tempfile
from pathlib import Path
import shutil
import pytest
from unittest.mock import patch
from functools import wraps

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def copy(func):
    """Decorator that does nothing and simply returns the wrapped function."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


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
    base_dir = tempfile.mkdtemp(prefix="copy_decorator_test_")
    workdir = Path(base_dir) / "workdir"
    output_dir = Path(base_dir) / "output"

    # Create test file structure
    workdir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    # Create a test file
    test_file_path = workdir / "source_file.txt"
    with open(test_file_path, "wb") as f:
        f.write(b"This is a test file for the @copy decorator.")

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


@pytest.fixture
def mock_context(setup_test_env):
    """Mock the simstack context for testing."""
    mock_ctx = MockContext(setup_test_env["workdir"])
    with patch("simstack.models.files.context", mock_ctx):
        yield mock_ctx


@pytest.fixture
def mock_job_submission():
    """Mock the job submission and status check functions."""
    with patch("simstack.models.files.submit_copy_job") as mock_submit, patch(
        "simstack.models.files.check_job_status"
    ) as mock_status:
        # Configure the mock job status to simulate completion after a few checks
        mock_status.side_effect = ["PENDING", "RUNNING", "COMPLETED"]

        # Let the submit function return a predictable job ID
        mock_submit.return_value = "test_job_id_12345"

        yield mock_submit, mock_status


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_copy_decorator_same_resource(setup_test_env, mock_context):
    """Test the @copy decorator for a transfer on the same resource."""

    # Define a function decorated with @copy
    @copy
    def transfer_same_resource():
        source_resource = "local"
        source_path = str(setup_test_env["test_file_path"])
        target_resource = "local"
        target_path = str(setup_test_env["output_dir"] / "copied_file.txt")
        return (source_resource, source_path, target_resource, target_path)

    # Call the decorated function
    result = transfer_same_resource()

    # Verify the result
    assert isinstance(result, str), "Result should be a string path"
    output_path = Path(result)
    assert output_path.exists(), "Output file should exist"
    assert output_path.name == "copied_file.txt", "Output filename should be correct"

    # Verify content was copied correctly
    with open(setup_test_env["test_file_path"], "rb") as src_file:
        src_content = src_file.read()
    with open(output_path, "rb") as dest_file:
        dest_content = dest_file.read()
    assert src_content == dest_content, "File content should match"


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_copy_decorator_different_resources(
    setup_test_env, mock_context, mock_job_submission
):
    """Test the @copy decorator for a transfer between different resources."""
    mock_submit, mock_status = mock_job_submission

    # Define a function decorated with @copy
    @copy
    def transfer_between_resources():
        source_resource = "local"
        source_path = str(setup_test_env["test_file_path"])
        target_resource = "remote1"
        target_path = "user/test/remote_file.txt"
        return (source_resource, source_path, target_resource, target_path)

    # Mock the minimal_route_finder function
    with patch("simstack.models.files.find_shortest_route") as mock_route_finder:
        # Set up route finder to return a valid route
        mock_route_finder.return_value = [
            ("local", "remote1", "local")  # Format: (source, target, executing_host)
        ]

        # Call the decorated function
        result = transfer_between_resources()

        # Verify the result
        assert isinstance(result, str), "Result should be a string path"
        assert (
            result == "remote1:user/test/remote_file.txt"
        ), "Result should include target resource and path"

        # Verify proper calls were made
        mock_route_finder.assert_called_once()
        mock_submit.assert_called_once_with(
            source_node="local",
            source_path=str(setup_test_env["test_file_path"]),
            target_node="remote1",
            target_path="user/test/remote_file.txt",
            executing_node="local",
        )
        assert mock_status.call_count > 0, "Job status should have been checked"


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_copy_decorator_multi_hop_route(
    setup_test_env, mock_context, mock_job_submission
):
    """Test the @copy decorator with a multi-hop route between resources."""
    mock_submit, mock_status = mock_job_submission

    # Define a function decorated with @copy
    @copy
    def transfer_via_multi_hop():
        source_resource = "remote2"
        source_path = "user/source/file.txt"
        target_resource = "remote1"
        target_path = "user/destination/file.txt"
        return (source_resource, source_path, target_resource, target_path)

    # Mock the minimal_route_finder function
    with patch("simstack.models.files.find_shortest_route") as mock_route_finder:
        # Set up route finder to return a multi-hop route
        mock_route_finder.return_value = [
            ("remote2", "local", "remote2"),  # First hop: remote2 -> local
            ("local", "remote1", "local"),  # Second hop: local -> remote1
        ]

        # Call the decorated function
        result = transfer_via_multi_hop()

        # Verify the result
        assert isinstance(result, str), "Result should be a string path"
        assert (
            result == "remote1:user/destination/file.txt"
        ), "Result should include target resource and path"

        # Verify proper calls were made
        mock_route_finder.assert_called_once()
        mock_submit.assert_called_once_with(
            source_node="remote2",
            source_path="user/source/file.txt",
            target_node="remote1",
            target_path="user/destination/file.txt",
            executing_node="remote2",  # First hop executor
        )
        assert mock_status.call_count > 0, "Job status should have been checked"


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_copy_decorator_no_route(setup_test_env, mock_context):
    """Test the @copy decorator with no available route."""

    # Define a function decorated with @copy
    @copy
    def transfer_no_route():
        source_resource = "unreachable"  # A resource with no defined routes
        source_path = "user/source/file.txt"
        target_resource = "remote1"
        target_path = "user/destination/file.txt"
        return (source_resource, source_path, target_resource, target_path)

    # Mock the minimal_route_finder function
    with patch("simstack.models.files.find_shortest_route") as mock_route_finder:
        # Set up route finder to return no route
        mock_route_finder.return_value = None

        # Call the decorated function and expect an error
        with pytest.raises(ValueError) as excinfo:
            transfer_no_route()

        # Verify proper error message
        assert "No route found" in str(excinfo.value)

        # Verify route finder was called
        mock_route_finder.assert_called_once()


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_copy_decorator_null_return(setup_test_env, mock_context):
    """Test the @copy decorator when the wrapped function returns None."""

    # Define a function decorated with @copy that returns None
    @copy
    def transfer_null():
        return None

    # Call the decorated function
    result = transfer_null()

    # Verify the result
    assert result is None, "Result should be None"


@pytest.mark.skip(reason="file copy tests must be fixed")
def test_copy_decorator_invalid_return(setup_test_env, mock_context):
    """Test the @copy decorator when the wrapped function returns invalid data."""

    # Define a function decorated with @copy that returns invalid data
    @copy
    def transfer_invalid():
        return "invalid_return_value"  # Not a tuple as expected

    # Call the decorated function and expect an error
    with pytest.raises(ValueError) as excinfo:
        transfer_invalid()

    # Verify proper error message
    assert "must return a tuple" in str(excinfo.value)


# Run the tests if this file is executed directly
if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
