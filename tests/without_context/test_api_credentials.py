import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from simstack.runner_api.credentials import (
    CONTAINER_CREDENTIAL_WRITER, CONTAINER_CREDENTIAL_PATH, CONTAINER_CREDENTIAL_TMPFS, credential_lock,
)
from simstack.runner_api.transport import RunnerAPIClient
from simstack.util.file_transfer_client import FileTransferClient


def test_existing_clients_reload_rotated_token_without_restart(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    server = "https://example.test/api"
    path.write_text(json.dumps({"server_url": server, "token": "first"}))
    inode = path.stat().st_ino
    monkeypatch.setenv("SIMSTACK_RUNNER_CREDENTIALS", str(path))
    api = RunnerAPIClient(server, "first")
    files = FileTransferClient(server_url=server, runner_token="first")
    assert api.token == files.runner_token == "first"
    with path.open("a+") as handle, credential_lock(handle, exclusive=True):
        handle.seek(0)
        handle.truncate()
        json.dump({"server_url": server, "token": "rotated"}, handle)
    assert path.stat().st_ino == inode
    assert api.token == files.runner_token == "rotated"


@pytest.mark.parametrize("kind", ["persistence", "files"])
def test_rejected_auth_waits_for_private_container_copy(tmp_path, monkeypatch, kind):
    path = tmp_path / "credentials.json"
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            auth = self.headers.get("Authorization")
            requests.append(auth)
            if auth != "Bearer rotated":
                path.write_text(json.dumps({"server_url": base, "token": "rotated"}))
                self.send_response(401)
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("SIMSTACK_RUNNER_CREDENTIALS", str(path))
    try:
        if kind == "persistence":
            result = RunnerAPIClient(base, "old").json_sync("POST", "/request", {})
        else:
            result = FileTransferClient(server_url=base, runner_token="old")._json_request("POST", "/request", {})
        assert result == {"accepted": True}
        assert requests == ["Bearer old", "Bearer rotated"]
    finally:
        server.shutdown()
        worker.join()


def test_container_credential_copy_stays_outside_workflow_data(tmp_path):
    from pathlib import PurePosixPath
    from simstack.core.run_docker import CONTAINER_WORKDIR
    assert not PurePosixPath(CONTAINER_CREDENTIAL_PATH).is_relative_to(CONTAINER_WORKDIR)
    assert all(flag in CONTAINER_CREDENTIAL_TMPFS for flag in ("noexec", "nosuid", "nodev", "size=1048576"))
    task = tmp_path / "readonly-root" / "mounted-workdir" / "task"
    task.mkdir(parents=True)
    private_mount = tmp_path / "auth-tmpfs"
    private_mount.mkdir()
    target = str(private_mount / "credentials.json")
    root = tmp_path / "readonly-root"
    root.chmod(0o555)
    try:
        result = subprocess.run([sys.executable, "-c", CONTAINER_CREDENTIAL_WRITER],
            env={**os.environ, "SIMSTACK_RUNNER_CREDENTIALS": target},
            input=json.dumps({"server_url": "https://test/api", "token": "private"}).encode(),
            capture_output=True, check=True)
        assert not result.stdout
        assert list(task.rglob("*")) == []
        assert stat.S_IMODE(Path(target).stat().st_mode) == 0o600
        assert json.loads(Path(target).read_text())["token"] == "private"
    finally:
        root.chmod(0o755)


def test_container_copy_keeps_secrets_out_of_exec_arguments(monkeypatch):
    from simstack.runner_api.credentials import copy_container_credentials
    observed = []
    def run(command, **kwargs):
        observed.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(subprocess, "run", run)
    assert copy_container_credentials("container-id", {"server_url": "https://test/api", "token": "private-token"})
    command, options = observed[0]
    assert "private-token" not in " ".join(command)
    assert json.loads(options["input"])["token"] == "private-token"
    assert "--user" not in command
