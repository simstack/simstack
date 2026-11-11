from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio


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


@pytest_asyncio.fixture(autouse=True)
async def test_runner(
    initialized_context, domain_bridge, tmp_path
) -> AsyncGenerator[subprocess.Popen[str] | None, None]:
    """Run the real HTTP-only Runner against the isolated test Server when enabled."""
    if not _is_enabled(os.environ.get("START_LOCAL_RUNNER")):
        yield None
        return

    import asyncio
    import uvicorn
    from simstack.server_api import ServerAPI
    from simstack.server_api.http_transport import runner_environment
    from server.routes import runner
    from server.runner_auth import get_current_runner_context

    app = domain_bridge.app
    app.include_router(runner.router)
    domain_bridge.auth.scopes.update({"runner:control", "runner:logs"})
    app.dependency_overrides[get_current_runner_context] = lambda: domain_bridge.auth
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server_url = "http://127.0.0.1:" + str(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    server_thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True)
    server_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert server.started, "Local HTTP Server did not start"

        executable_name = "simstack_runner.exe" if os.name == "nt" else "simstack_runner"
        # Do not resolve the virtualenv Python symlink: its parent is where console
        # entrypoints are installed, while the resolved interpreter lives outside it.
        runner_executable = Path(sys.executable).parent / executable_name
        if not runner_executable.is_file():
            pytest.fail(
                f"simstack_runner console entrypoint not found: {runner_executable}"
            )

        repository_root = Path(__file__).resolve().parents[3]
        runner_project_root = tmp_path / "runner-project"
        runner_project_root.mkdir()
        runner_workdir = runner_project_root / "work"
        runner_workdir.mkdir()
        (runner_project_root / "config.toml").write_text("", encoding="utf-8")
        (runner_project_root / "sitecustomize.py").write_text(
            "import sys\n"
            "class NoDatabaseImports:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in {'bson','pymongo','motor','odmantic'}:\n"
            "            raise AssertionError('Runner imported database dependency: ' + name)\n"
            "sys.meta_path.insert(0, NoDatabaseImports())\n", encoding="utf-8")

        command = [
            str(runner_executable),
            "--project-root",
            str(runner_project_root),
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
        process_environment = runner_environment()
        for key in ("SIMSTACK_API_TEST_MONGO", "SIMSTACK_TEST_DB_CONNECTION_STRING", "SIMSTACK_TEST_DB"):
            process_environment.pop(key, None)
        process_environment.update(SIMSTACK_SERVER_URL=server_url, SIMSTACK_RUNNER_TOKEN="local-test-resource-token",
            SIMSTACK_RUNNER_WORKDIR=str(runner_workdir))
        process_environment["PYTHONUNBUFFERED"] = "1"
        python_paths = [str(runner_project_root), str(repository_root), str(repository_root / "src")]
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
        process.simstack_user_api = ServerAPI.for_user(server_url, "local-test-user-token", resource="test")
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
            await asyncio.sleep(.1)

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
    finally:
        server.should_exit = True
        await asyncio.to_thread(server_thread.join, 5)
        listener.close()
