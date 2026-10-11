import asyncio
import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from odmantic import ObjectId

from simstack.core.definitions import TaskStatus
from simstack.core.node import Node
from simstack.core.services import fail_children
from simstack.core.services.fail_children import (
    fail_and_terminate_children,
    fail_and_terminate_task,
)


class _Finished:
    def __init__(self) -> None:
        self.returncode = 0

    async def communicate(self):
        return b"", b""


class _CancelledProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None
        self.waited = False

    async def communicate(self):
        raise asyncio.CancelledError()

    async def wait(self):
        self.waited = True
        self.returncode = -15
        return self.returncode


class _Collection:
    def __init__(self, entries) -> None:
        self.entries = {entry.id: entry for entry in entries}

    async def find_one_and_update(self, query, update):
        entry = self.entries.get(query["_id"])
        if entry is None:
            return None
        if entry.status.value not in query["status"]["$in"]:
            return None
        entry.status = TaskStatus.FAILED
        entry.error = update["$set"]["error"]
        entry.completed_at = update["$set"]["completed_at"]
        return entry


class _Database:
    def __init__(self, entries) -> None:
        self.entries = list(entries)
        self.collection = _Collection(self.entries)
        self.saved = []

    async def load_task_by_id(self, task_id):
        for entry in self.entries:
            if entry.id == task_id:
                return entry
        return None

    async def find(self, model, query):
        return list(self.entries)

    async def save(self, entry):
        self.saved.append(entry)
        return entry

    def get_collection(self, model):
        return self.collection


def _entry(**overrides):
    values = {
        "id": ObjectId(),
        "name": "partial",
        "status": TaskStatus.RUNNING,
        "job_id": None,
        "process_id": None,
        "error": None,
        "completed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(database, tmp_path):
    return SimpleNamespace(db=database, config=SimpleNamespace(workdir=tmp_path))


@pytest.mark.asyncio
async def test_failed_parent_kills_running_children_and_leaves_completed(monkeypatch, tmp_path):
    parent_id = ObjectId()
    running = _entry(job_id="4321", process_id=99)
    completed = _entry(status=TaskStatus.COMPLETED, error=None)
    database = _Database([running, completed])
    cidfile = tmp_path / running.name / str(running.id) / ".docker_cid"
    cidfile.parent.mkdir(parents=True)
    cidfile.write_text("abc123def\n", encoding="utf-8")
    commands = []
    killed = []

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return _Finished()

    monkeypatch.setattr(fail_children, "context", _context(database, tmp_path))
    monkeypatch.setattr(fail_children.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        fail_children, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )

    await fail_and_terminate_children(parent_id, runner_pid=os.getpid())

    assert commands == [("scancel", "4321"), ("docker", "kill", "abc123def")]
    assert killed == [99]
    assert running.status == TaskStatus.FAILED
    assert running.error == f"Terminated because parent task {parent_id} failed"
    assert isinstance(running.completed_at, datetime)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.error is None


@pytest.mark.asyncio
async def test_terminate_task_does_not_kill_the_runner(monkeypatch, tmp_path):
    runner_pid = os.getpid()
    entry = _entry(process_id=runner_pid)
    database = _Database([entry])
    killed = []

    monkeypatch.setattr(fail_children, "context", _context(database, tmp_path))
    monkeypatch.setattr(
        fail_children.asyncio,
        "create_subprocess_exec",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("command ran")),
    )
    monkeypatch.setattr(
        fail_children, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )

    marked = await fail_and_terminate_task(
        entry,
        error=f"Terminated because parent task {ObjectId()} failed",
        runner_pid=runner_pid,
        extra_pids=(runner_pid,),
    )

    assert marked is True
    assert killed == []
    assert entry.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_cancelled_run_node_kills_and_fails_the_child(monkeypatch, tmp_path):
    parent_id = ObjectId()
    entry = _entry()
    database = _Database([entry])
    proc = _CancelledProcess(4321)
    killed = []

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return proc

    monkeypatch.setattr(fail_children, "context", _context(database, tmp_path))
    monkeypatch.setattr(
        fail_children, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=False,
            config=SimpleNamespace(
                resource="local",
                project_root=tmp_path,
                python_paths=[],
                connection_string=None,
                db_name=None,
                workdir=tmp_path,
            ),
            db=database,
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    execution_node = Node.__new__(Node)
    execution_node.name = "partial"
    execution_node.registry_entry = entry
    execution_node.parent_id = parent_id
    execution_node.parameters = SimpleNamespace(resource="local")

    with pytest.raises(asyncio.CancelledError):
        await execution_node.run_node_as_process()

    assert killed == [4321]
    assert proc.waited is True
    assert entry.status == TaskStatus.FAILED
    assert entry.error == f"Terminated because parent task {parent_id} failed"


@pytest.mark.asyncio
async def test_set_status_failed_terminates_children(monkeypatch, tmp_path):
    parent_id = ObjectId()
    running = _entry(process_id=41)
    database = _Database([running])
    killed = []
    fake_context = _context(database, tmp_path)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    monkeypatch.setattr(fail_children, "context", fake_context)
    monkeypatch.setattr(
        fail_children, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )
    node = Node.__new__(Node)
    node.name = "orchestrator"
    node.registry_entry = SimpleNamespace(
        id=parent_id,
        name="orchestrator",
        status=TaskStatus.RUNNING,
    )

    await node.set_status(TaskStatus.FAILED)

    assert node.registry_entry.status == TaskStatus.FAILED
    assert killed == [41]
    assert running.status == TaskStatus.FAILED
    assert running.error == f"Terminated because parent task {parent_id} failed"
