import os
import signal
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.node import Node
from simstack.core.services import task_termination
from simstack.core.services.task_termination import (
    TERMINATED_BY_USER,
    _kill_process_tree,
    terminate_task,
)


class _Finished:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self):
        return b"", b""


class _Collection:
    def __init__(self, entry) -> None:
        self.entry = entry

    async def find_one_and_update(self, query, update):
        if self.entry.status != TaskStatus.TERMINATING:
            return None
        if query.get("status") != TaskStatus.TERMINATING.value:
            return None
        self.entry.status = TaskStatus.FAILED
        self.entry.error = update["$set"]["error"]
        self.entry.completed_at = update["$set"]["completed_at"]
        return self.entry


class _Database:
    def __init__(self, entry) -> None:
        self.entry = entry
        self.collection = _Collection(entry)

    async def load_task_by_id(self, task_id):
        assert task_id == self.entry.id
        return self.entry

    def get_collection(self, model):
        return self.collection


def _entry(**overrides):
    values = {
        "id": "task-1",
        "name": "vasp_run",
        "status": TaskStatus.TERMINATING,
        "job_id": None,
        "process_id": None,
        "error": None,
        "completed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_set_status_does_not_replace_terminating_with_running():
    entry = SimpleNamespace(status=TaskStatus.TERMINATING, id="task-1")
    node = Node.__new__(Node)
    node.registry_entry = entry
    with pytest.raises(RuntimeError, match="Terminated by user"):
        await node.set_status(TaskStatus.RUNNING)
    assert entry.status == TaskStatus.TERMINATING


def test_kill_process_tree_uses_taskkill_on_windows(monkeypatch):
    commands = []

    def fake_run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(task_termination.platform, "system", lambda: "Windows")
    monkeypatch.setattr(task_termination.subprocess, "run", fake_run)

    _kill_process_tree(55)

    assert commands == [["taskkill", "/PID", "55", "/T", "/F"]]


def test_kill_process_tree_kills_children_then_process_on_unix(monkeypatch):
    killed = []
    monkeypatch.setattr(task_termination.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        task_termination,
        "_child_pids",
        lambda process_id: [7] if process_id == 55 else [],
    )
    monkeypatch.setattr(
        task_termination.os,
        "kill",
        lambda process_id, sig: killed.append((process_id, sig)),
    )

    _kill_process_tree(55)

    sigkill = getattr(signal, "SIGKILL", 9)
    assert killed == [(7, sigkill), (55, sigkill)]


@pytest.mark.asyncio
async def test_terminate_task_scancels_kills_docker_and_process_tree(
    monkeypatch, tmp_path
):
    entry = _entry(job_id="4321", process_id=99)
    cidfile = tmp_path / entry.name / str(entry.id) / ".docker_cid"
    cidfile.parent.mkdir(parents=True)
    cidfile.write_text("abc123def\n", encoding="utf-8")
    commands = []

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return _Finished()

    killed = []
    monkeypatch.setattr(
        task_termination,
        "context",
        SimpleNamespace(
            db=_Database(entry),
            config=SimpleNamespace(workdir=tmp_path),
        ),
    )
    monkeypatch.setattr(task_termination.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )

    await terminate_task(entry, runner_pid=os.getpid())

    assert commands == [("scancel", "4321"), ("docker", "kill", "abc123def")]
    assert killed == [99]
    assert entry.status == TaskStatus.FAILED
    assert entry.error == TERMINATED_BY_USER
    assert isinstance(entry.completed_at, datetime)


@pytest.mark.asyncio
async def test_terminate_task_does_not_kill_the_runner(monkeypatch, tmp_path):
    runner_pid = os.getpid()
    entry = _entry(process_id=runner_pid)
    killed = []
    commands = []

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return _Finished()

    monkeypatch.setattr(
        task_termination,
        "context",
        SimpleNamespace(
            db=_Database(entry),
            config=SimpleNamespace(workdir=tmp_path),
        ),
    )
    monkeypatch.setattr(task_termination.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )

    await terminate_task(entry, runner_pid=runner_pid)

    assert commands == []
    assert killed == []
    assert entry.status == TaskStatus.FAILED
    assert entry.error == TERMINATED_BY_USER


@pytest.mark.asyncio
async def test_terminate_task_marks_failed_when_nothing_is_running(monkeypatch, tmp_path):
    entry = _entry()
    commands = []

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return _Finished()

    monkeypatch.setattr(
        task_termination,
        "context",
        SimpleNamespace(
            db=_Database(entry),
            config=SimpleNamespace(workdir=tmp_path),
        ),
    )
    monkeypatch.setattr(task_termination.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        task_termination,
        "_kill_process_tree",
        lambda process_id: (_ for _ in ()).throw(
            AssertionError("process tree kill without a process_id")
        ),
    )

    await terminate_task(entry, runner_pid=os.getpid())

    assert commands == []
    assert entry.status == TaskStatus.FAILED
    assert entry.error == TERMINATED_BY_USER


@pytest.mark.asyncio
async def test_terminate_task_leaves_a_task_that_is_no_longer_terminating(
    monkeypatch, tmp_path
):
    entry = _entry(status=TaskStatus.COMPLETED, error=None)
    monkeypatch.setattr(
        task_termination,
        "context",
        SimpleNamespace(
            db=_Database(entry),
            config=SimpleNamespace(workdir=tmp_path),
        ),
    )
    monkeypatch.setattr(
        task_termination.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("kill command ran")),
    )

    await terminate_task(entry, runner_pid=os.getpid())

    assert entry.status == TaskStatus.COMPLETED
    assert entry.error is None
