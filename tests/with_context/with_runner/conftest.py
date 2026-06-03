import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from simstack.core.context import context
from simstack.util.project_root_finder import find_project_root
from tests.with_context.with_runner.runner_smoke_toml import write_runner_smoke_toml


@pytest.fixture(scope="session", autouse=True)
def test_runner(initialized_context):
    """
    Start a real runner process against the shared test MongoDB instance.
    """
    start_local_runner = os.environ.get("START_LOCAL_RUNNER", "True").lower()
    if start_local_runner == "false":
        pytest.fail("START_LOCAL_RUNNER=false disables the required runner smoke setup.", pytrace=False)

    # allowed_resources.add_resource("test_resource")
    root = Path(find_project_root(skip_files=()))
    connection_string = os.environ["SIMSTACK_TEST_DB_CONNECTION_STRING"].strip()
    test_database_name = os.environ.get("SIMSTACK_TEST_DB", "ui_testing").strip()
    runner_config_path = root / "runner-test.simstack.toml"
    runner_pid_path = root / "test_workdir" / "runner_test.pid"
    # Keep the parent smoke bootstrap and the child runner on the same test TOML shape.
    write_runner_smoke_toml(
        runner_config_path,
        project_root=root,
        workdir_self=context.config.workdir,
        connection_string=connection_string,
        database_name=test_database_name,
        use_db=True,
    )

    command = [
        sys.executable,
        "-m",
        "simstack.core.runner",
        "--resource",
        "test",
        "--no-pull",
        "--detach",
        "false",
        "--polling-interval",
        "1",
        "--connection-string",
        connection_string,
        "--db-name",
        test_database_name,
        "--config",
        str(runner_config_path),
    ]

    env = os.environ.copy()
    # The runner subprocess imports simstack as a plain Python process rather than
    # via the pytest session, so we pin both the project root and Python path here
    # to keep module discovery and config resolution identical to the parent test.
    env["SIMSTACK_PROJECT_ROOT"] = str(root)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(root / "src") if not existing_pythonpath else f"{root / 'src'}{os.pathsep}{existing_pythonpath}"

    print(f"Starting subprocess with command: {str(command)}")

    # Remove possibly stale pid file
    try:
        runner_pid_path.unlink()
    except FileNotFoundError:
        pass

    # Start the process with non-blocking pipes
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Queues to store output
    stdout_queue: queue.Queue[str] = queue.Queue()
    stderr_queue: queue.Queue[str] = queue.Queue()

    def _pump_stream(stream, target_queue: queue.Queue[str], prefix: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                if line:
                    stripped_line = line.rstrip()
                    target_queue.put(stripped_line)
                    print(f"[{prefix}] {stripped_line}")
        finally:
            if stream:
                stream.close()

    # Start threads to read output immediately
    stdout_thread = threading.Thread(
        target=_pump_stream,
        args=(process.stdout, stdout_queue, "SUBPROCESS STDOUT"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pump_stream,
        args=(process.stderr, stderr_queue, "SUBPROCESS STDERR"),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    # Add queues to process object so tests can access them
    process.stdout_queue = stdout_queue
    process.stderr_queue = stderr_queue

    # Fail fast with the runner output if startup crashes; otherwise later node
    # submission failures are much harder to diagnose from CI logs.
    time.sleep(2)
    if process.poll() is not None:
        runner_output = []
        while not stderr_queue.empty():
            runner_output.append(stderr_queue.get())
        while not stdout_queue.empty():
            runner_output.append(stdout_queue.get())
        raise RuntimeError(
            "Runner process exited during test setup.\n"
            + "\n".join(runner_output)
        )

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
            process.wait(timeout=5)

    # Wait for threads to finish
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)

    # RunnerManager writes a per-resource pid file into the resource workdir.
    # The smoke fixture owns that lifecycle too, otherwise repeated local runs
    # leave a stale runner_test.pid behind.
    try:
        runner_pid_path.unlink()
    except FileNotFoundError:
        pass

    try:
        runner_config_path.unlink()
    except FileNotFoundError:
        pass

    print("Subprocess cleanup complete")
    # allowed_resources.remove_resource("test_resource")

