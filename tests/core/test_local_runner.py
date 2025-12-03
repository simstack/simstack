import pytest
import subprocess


@pytest.fixture(scope="function")
def runner_process(test_runner) -> subprocess.Popen:
    """



    Function-scoped fixture that provides access to the running runner process.
    Uses the session-scoped local_runner fixture.
    """
    # Ensure the runner is still running
    if test_runner.poll() is not None:
        raise RuntimeError("Runner process has died unexpectedly")

    return test_runner


# Example usage in tests:
@pytest.mark.local_runner
def test_runner_is_running(runner_process):
    """Test that the runner process is running correctly."""
    assert runner_process.poll() is None, "Runner process should be running"
