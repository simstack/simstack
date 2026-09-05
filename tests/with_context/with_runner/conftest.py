from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Generator

import pytest


_RUNNER_READY_MESSAGE = "Service JobPolling started."
_RUNNER_START_TIMEOUT_SECONDS = 20


def _is_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _runner_output(process: subprocess.Popen[str]) -> str:
    lines = getattr(process, "simstack_output_lines", ())
    return "\n".join(lines)


def _stop_runner(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.fixture(scope="session", autouse=True)
def test_runner(
    initialized_context, tmp_path_factory
) -> Generator[subprocess.Popen[str] | None, None, None]:
    """Start a real runner only for the explicitly enabled MongoDB integration gate."""
    if not _is_enabled(os.environ.get("START_LOCAL_RUNNER")):
        yield None
        return

    connection_string = os.environ.get("SIMSTACK_TEST_DB_CONNECTION_STRING", "").strip()
    database_name = os.environ.get("SIMSTACK_TEST_DB", "").strip()
    if (
        not connection_string
        or connection_string.lower() == "none"
        or not database_name
    ):
        pytest.fail(
            "START_LOCAL_RUNNER requires SIMSTACK_TEST_DB_CONNECTION_STRING and "
            "SIMSTACK_TEST_DB for a real disposable MongoDB database."
        )

    executable_name = "simstack_runner.exe" if os.name == "nt" else "simstack_runner"
    # Do not resolve the virtualenv Python symlink: its parent is where console
    # entrypoints are installed, while the resolved interpreter lives outside it.
    runner_executable = Path(sys.executable).parent / executable_name
    if not runner_executable.is_file():
        pytest.fail(
            f"simstack_runner console entrypoint not found: {runner_executable}"
        )

    repository_root = Path.cwd().resolve()
    runner_project_root = tmp_path_factory.mktemp("simstack_runner_project")
    runner_workdir = runner_project_root / "work"
    runner_workdir.mkdir()
    (runner_project_root / "simstack.toml").write_text(
        "\n".join(
            [
                "[parameters.general]",
                f"workdir_self = {json.dumps(str(runner_workdir))}",
                "",
                "[parameters.db]",
                f"database = {json.dumps(database_name)}",
                f"connection_string = {json.dumps(connection_string)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (runner_project_root / "config.toml").write_text("", encoding="utf-8")

    command = [
        str(runner_executable),
        "--resource",
        "test",
        "--polling-interval",
        "1",
        "--detach",
        "false",
        "--pull",
        "false",
        "--file-transfer",
        "false",
    ]
    process_environment = os.environ.copy()
    process_environment["PYTHONUNBUFFERED"] = "1"
    process_environment["SIMSTACK_PROJECT_ROOT"] = str(runner_project_root)
    python_paths = [str(repository_root), str(repository_root / "src")]
    if process_environment.get("PYTHONPATH"):
        python_paths.append(process_environment["PYTHONPATH"])
    process_environment["PYTHONPATH"] = os.pathsep.join(python_paths)

    process = subprocess.Popen(
        command,
        cwd=repository_root,
        env=process_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_lines: deque[str] = deque(maxlen=200)
    process.simstack_output_lines = output_lines
    ready = threading.Event()

    def read_output() -> None:
        assert process.stdout is not None
        for raw_line in iter(process.stdout.readline, ""):
            line = raw_line.rstrip()
            if not line:
                continue
            output_lines.append(line)
            print(f"[SIMSTACK RUNNER]: {line}")
            if _RUNNER_READY_MESSAGE in line:
                ready.set()
        process.stdout.close()

    output_thread = threading.Thread(target=read_output, daemon=True)
    output_thread.start()

    deadline = time.monotonic() + _RUNNER_START_TIMEOUT_SECONDS
    while not ready.is_set() and time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            output_thread.join(timeout=1)
            pytest.fail(
                f"simstack_runner exited before readiness with code {return_code}.\n"
                f"{_runner_output(process)}"
            )
        ready.wait(timeout=0.1)

    if not ready.is_set():
        _stop_runner(process)
        output_thread.join(timeout=1)
        pytest.fail(
            "simstack_runner did not report readiness within "
            f"{_RUNNER_START_TIMEOUT_SECONDS} seconds.\n{_runner_output(process)}"
        )

    try:
        yield process
        unexpected_return_code = process.poll()
    finally:
        _stop_runner(process)
        output_thread.join(timeout=1)

    if unexpected_return_code is not None:
        pytest.fail(
            f"simstack_runner exited unexpectedly with code {unexpected_return_code}.\n"
            f"{_runner_output(process)}"
        )
