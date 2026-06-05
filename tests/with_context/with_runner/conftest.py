import os
import queue
import sys
import threading
from pathlib import Path

import pytest

from simstack.core.context import context
from simstack.util.project_root_finder import find_project_root


@pytest.fixture(scope="session", autouse=True)
def test_runner(initialized_context):
    """
    Fixture to run and manage the test runner process.
    """

    start_local_runner = os.environ.get("START_LOCAL_RUNNER", "True").lower()
    if start_local_runner == "false":
        return

    import subprocess
    import platform
    import time

    import logging

    logger = logging.getLogger("simstack-runner")

    # allowed_resources.add_resource("test_resource")
    root = Path(find_project_root())
    command = root / "src" / "simstack" / "core" / "simstack_runner.py"

    print("environment_start", context.config.environment_start)

    # Cross-platform command chaining
    system = platform.system().lower()
    env_start = (
        context.config.environment_start.strip()
        if context.config.environment_start
        else ""
    )

    connection_string = os.environ.get("SIMSTACK_TEST_DB_CONNECTION_STRING", "none")
    test_database_name = os.environ.get("SIMSTACK_TEST_DB", "none")

    logger.info(
        f"Test context initialized with real MongoDB database at: {connection_string} and test database: {test_database_name}"
    )

    shared_args = f"uv run simstack_runner --resource test --no-pull --connection-string {connection_string} --db-name {test_database_name}"
    if system == "windows":
        if env_start:
            command_string = f'cmd /c "{env_start} &&  {shared_args}"'
        else:
            command_string = f'cmd /c "{shared_args}"'
    else:
        if env_start:
            command_string = (
                f"{env_start} && {sys.executable} {command} --resource tests --no-pull"
            )
        else:
            command_string = f"{sys.executable} {command} --resource tests --no-pull"

    print(f"Starting subprocess with command: {command_string}")

    # Start the process with non-blocking pipes
    process = subprocess.Popen(
        command_string,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0,
    )  # Unbuffered

    # Queues to store output
    stdout_queue = queue.Queue()
    stderr_queue = queue.Queue()

    def read_stdout():
        import logging

        logger = logging.getLogger("simstack-runner")
        try:
            for line in iter(process.stdout.readline, ""):
                if line:
                    line = line.strip()
                    stdout_queue.put(line)
                    print(f"[SUBPROCESS STDOUT]: {line}")
        except Exception as e:
            print(f"Error reading stdout: {e}")
        finally:
            if process.stdout:
                process.stdout.close()

    def read_stderr():
        import logging

        logger = logging.getLogger("simstack-runner")
        try:
            for line in iter(process.stderr.readline, ""):
                if line:
                    line = line.strip()
                    stderr_queue.put(line)
                    print(f"[SUBPROCESS STDERR]: {line}")
        except Exception as e:
            print(f"Error reading stderr: {e}")
        finally:
            if process.stderr:
                process.stderr.close()

    # Start threads to read output immediately
    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    # Add queues to process object so tests can access them
    process.stdout_queue = stdout_queue
    process.stderr_queue = stderr_queue

    # Give the process a moment to start
    time.sleep(1)

    # Check if process started successfully
    if process.poll() is not None:
        print(f"Process exited early with code: {process.returncode}")
        # Try to get any error output
        time.sleep(0.5)  # Give threads time to read final output
        while not stderr_queue.empty():
            print(f"[SUBPROCESS STDERR]: {stderr_queue.get()}")
        while not stdout_queue.empty():
            print(f"[SUBPROCESS STDOUT]: {stdout_queue.get()}")
    else:
        print("Process started successfully")

    yield process

    # Cleanup: terminate the process
    print("Cleaning up subprocess...")
    if process.poll() is None:  # Process is still running
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("Process didn't terminate gracefully, killing...")
            process.kill()
            process.wait()

    # Wait for threads to finish
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)

    print("Subprocess cleanup complete")
    # allowed_resources.remove_resource("test_resource")
