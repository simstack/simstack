"""Durable claim receipts and execution identity for API workers."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

_execution = ContextVar("simstack_execution", default=None)


def _directory(api):
    workdir = os.environ.get("SIMSTACK_RUNNER_WORKDIR")
    credentials = os.environ.get("SIMSTACK_RUNNER_CREDENTIALS")
    root = Path(workdir) if workdir else Path(credentials).parent if credentials else Path.home()/".local/state/simstack"
    key = hashlib.sha256(api.server_url.encode()).hexdigest()[:16]
    directory = root/".claims"/key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _write(path, value):
    temporary = path.with_name(path.name+"."+uuid4().hex+".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        temporary.chmod(0o600)
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def receipt(api, task_id, *, begin=False, revision=None, generation=None):
    if len(str(task_id)) != 24 or any(c not in "0123456789abcdef" for c in str(task_id)):
        raise ValueError("Invalid claim task identity")
    path = _directory(api)/(str(task_id)+".json")
    from simstack.runner_api.credentials import credential_lock
    with path.with_suffix(".lock").open("a+") as lock, credential_lock(lock, exclusive=True):
        value = json.loads(path.read_text()) if path.exists() else None
        if begin and (value is None or value.get("state") != "pending" or value.get("generation") != generation):
            value = {"task_id": str(task_id), "claim_id": str(uuid4()), "revision": revision, "generation": generation, "state": "pending"}
            _write(path, value)
    return value


def finish(api, value, *, claimed):
    path = _directory(api)/(value["task_id"]+".json")
    from simstack.runner_api.credentials import credential_lock
    with path.with_suffix(".lock").open("a+") as lock, credential_lock(lock, exclusive=True):
        current = json.loads(path.read_text()) if path.exists() else None
        if current and current["claim_id"] == value["claim_id"]:
            if claimed:
                _write(path, {**value, "state": "claimed"})
            else:
                path.unlink()


def pending(api):
    for path in _directory(api).glob("*.json"):
        value = json.loads(path.read_text())
        if value.get("state") in {"pending", "claimed"}:
            yield value


def current_execution():
    value = _execution.get()
    if value:
        return value
    task_id = os.environ.get("SIMSTACK_EXECUTION_TASK")
    claim_id = os.environ.get("SIMSTACK_EXECUTION_CLAIM")
    if not task_id or not claim_id:
        return None
    value = {"task_id": task_id, "claim_id": claim_id}
    if os.environ.get("SIMSTACK_EXECUTION_START"):
        value["start_id"] = os.environ["SIMSTACK_EXECUTION_START"]
    return value


def execution_for(task_id):
    current = current_execution()
    if current and current["task_id"] == str(task_id):
        return current
    from simstack.runner_api.transport import RunnerAPIClient
    try:
        value = receipt(RunnerAPIClient.from_environment(), str(task_id))
    except ValueError:
        return None
    if value and value.get("state") == "claimed":
        return {"task_id": str(task_id), "claim_id": value["claim_id"]}
    return None


@contextmanager
def task_execution(entry):
    existing = current_execution()
    claim_id = getattr(entry, "_execution_claim_id", None)
    value = ({"task_id": str(entry.id), "claim_id": claim_id} if claim_id else execution_for(str(entry.id)))
    if existing and existing.get("task_id") == str(entry.id):
        value = existing
    if value and "start_id" not in value:
        start_id = getattr(entry, "_execution_start_id", None) or str(uuid4())
        object.__setattr__(entry, "_execution_start_id", start_id)
        value = {**value, "start_id": start_id}
    token = _execution.set(value)
    try:
        yield
    finally:
        _execution.reset(token)
