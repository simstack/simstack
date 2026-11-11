"""Durable external submission intents; no credentials or task snapshots."""

from __future__ import annotations

import os
import subprocess
from uuid import uuid4

from simstack.runner_runtime.launch_claims import _actor, _directory, _receipt_path, _write


def _path(server, task_id):
    path = _directory(server.requests.server_url) / "slurm-submissions"
    path.mkdir(exist_ok=True, mode=0o700)
    return path / _receipt_path(server, task_id).name


def read_intent(server, task_id):
    import json

    path = _path(server, task_id)
    return json.loads(path.read_text()) if path.exists() else None


def begin_intent(server, entry):
    execution = server.tasks.execution(entry)
    value = read_intent(server, entry.id)
    if value is not None and value["execution"] == execution:
        return value
    value = {
        "actor": _actor(server),
        "execution": execution,
        "resource": getattr(entry, "_execution_resource", None)
        or str(entry.parameters.resource),
        "submission_id": str(uuid4()),
        "phase": "prepared",
    }
    return update_intent(server, value)


def update_intent(server, value, **changes):
    value = {**value, **changes}
    if "process_id" in changes:
        from simstack.runner_runtime.pending_execution_failures import cleanup_scope
        value["scope"] = cleanup_scope()
    _write(_path(server, value["execution"]["task_id"]), value)
    return value


def forget_intent(server, value):
    _path(server, value["execution"]["task_id"]).unlink(missing_ok=True)


def pending_intents(server, resource):
    import json

    directory = _directory(server.requests.server_url) / "slurm-submissions"
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            value = json.loads(path.read_text())
            if value.get("actor") == _actor(server) and value["resource"] == str(resource):
                yield value


def process_identity(pid):
    """Process birth identity works on both Linux and macOS Slurm submit hosts."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def submitter_alive(value):
    from simstack.runner_runtime.pending_execution_failures import cleanup_scope
    # A PID lookup proves nothing outside the scope that created the process.
    scope = value.get("scope")
    if scope is None or scope != cleanup_scope():
        return None
    pid = value.get("process_id")
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    birth = value.get("process_birth")
    current = process_identity(pid)
    if current is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return None
    return current == birth if birth is not None else None
