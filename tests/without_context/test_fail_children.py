import asyncio
import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from bson import ObjectId as BsonObjectId
from odmantic import ObjectId

from simstack.core.definitions import TaskStatus
from simstack.core.node import Node
from simstack.core.services import fail_children, task_termination
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
        status = query["status"]
        if isinstance(status, dict) and entry.status.value not in status["$in"]:
            return None
        if isinstance(status, str) and entry.status.value != status:
            return None
        if "parent_ids" in query and entry.parent_ids != query["parent_ids"]:
            return None
        fields = update["$set"]
        entry.status = TaskStatus(fields["status"])
        entry.error = fields["error"]
        if "completed_at" in fields:
            entry.completed_at = fields["completed_at"]
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
        parents = query["parent_ids"]["$in"]
        return [entry for entry in self.entries if any(parent in entry.parent_ids for parent in parents)]

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
        "parent_ids": [],
        "parameters": SimpleNamespace(resource="self"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(monkeypatch, database, tmp_path):
    fake_context = SimpleNamespace(db=database, config=SimpleNamespace(workdir=tmp_path))
    monkeypatch.setattr(fail_children, "context", fake_context)
    monkeypatch.setattr(task_termination, "context", fake_context)
    return fake_context


@pytest.mark.asyncio
async def test_assigned_worker_stops_child_then_requests_grandchild_stop(monkeypatch, tmp_path):
    parent_id = ObjectId()
    named_resource = SimpleNamespace(resource="local")
    running = _entry(
        parent_ids=[parent_id], parameters=named_resource, job_id="4321", process_id=99,
    )
    completed = _entry(parent_ids=[parent_id], status=TaskStatus.COMPLETED)
    grandchild = _entry(parent_ids=[running.id], parameters=named_resource)
    database = _Database([running, completed, grandchild])
    cidfile = tmp_path / running.name / str(running.id) / ".docker_cid"
    cidfile.parent.mkdir(parents=True)
    cidfile.write_text("abc123def\n", encoding="utf-8")
    commands = []
    killed = []

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return _Finished()

    _context(monkeypatch, database, tmp_path)
    monkeypatch.setattr(task_termination.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda pid: killed.append(pid) or True,
    )

    await fail_and_terminate_children(parent_id, runner_pid=os.getpid())

    assert commands == []
    assert killed == []
    assert running.status == TaskStatus.TERMINATING
    assert grandchild.status == TaskStatus.RUNNING

    await task_termination.terminate_task(running, runner_pid=os.getpid())

    assert commands == [("scancel", "4321"), ("docker", "kill", "abc123def")]
    assert killed == [99]
    assert running.status == TaskStatus.FAILED
    assert running.error == f"Terminated because parent task {parent_id} failed"
    assert isinstance(running.completed_at, datetime)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.error is None
    assert grandchild.status == TaskStatus.TERMINATING
    assert grandchild.error == f"Terminated because parent task {running.id} failed"


@pytest.mark.asyncio
async def test_loaded_bson_parent_id_terminates_children(monkeypatch, tmp_path):
    parent_id = BsonObjectId("6acc94fd41c9db0721eb8ac0")
    assert not isinstance(parent_id, ObjectId)
    running = _entry(parent_ids=[parent_id], process_id=41)
    database = _Database([running])
    killed = []

    _context(monkeypatch, database, tmp_path)
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda pid: killed.append(pid) or True,
    )

    await fail_and_terminate_children(parent_id, runner_pid=os.getpid())

    assert killed == [41]
    assert running.status == TaskStatus.FAILED
    assert running.error == f"Terminated because parent task {parent_id} failed"


@pytest.mark.asyncio
async def test_terminate_task_does_not_kill_the_runner(monkeypatch, tmp_path):
    runner_pid = os.getpid()
    entry = _entry(process_id=runner_pid)
    database = _Database([entry])
    killed = []

    _context(monkeypatch, database, tmp_path)
    monkeypatch.setattr(
        task_termination.asyncio,
        "create_subprocess_exec",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("command ran")),
    )

    def kill_local(pid):
        killed.append(pid)
        return True

    monkeypatch.setattr(fail_children, "kill_process_tree", kill_local)
    monkeypatch.setattr(task_termination, "_kill_process_tree", kill_local)

    marked = await fail_and_terminate_task(
        entry,
        error=f"Terminated because parent task {ObjectId()} failed",
        runner_pid=runner_pid,
        local_process=_CancelledProcess(runner_pid),
    )

    assert marked is True
    assert killed == []
    assert entry.status == TaskStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "lookup", "shared", "database"])
@pytest.mark.parametrize("resource", ["self", "local"])
async def test_cancelled_run_node_requests_child_stop(
    monkeypatch, tmp_path, caplog, failure, resource,
):
    parent_id = ObjectId()
    other_parent = _entry()
    parent_ids = [parent_id, other_parent.id] if failure == "shared" else [parent_id]
    entry = _entry(
        parent_ids=parent_ids, parameters=SimpleNamespace(resource=resource), process_id=4321,
    )
    database = _Database([entry, other_parent])

    async def unavailable(*args, **kwargs):
        raise ConnectionError("child database unavailable")

    if failure == "lookup":
        monkeypatch.setattr(database, "load_task_by_id", unavailable)
    if failure == "database":
        monkeypatch.setattr(database.collection, "find_one_and_update", unavailable)
    proc = _CancelledProcess(4321)
    killed = []

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return proc

    def kill_local(pid):
        killed.append(pid)
        return True

    monkeypatch.setattr(fail_children, "kill_process_tree", kill_local)
    monkeypatch.setattr(task_termination, "_kill_process_tree", kill_local)
    _context(monkeypatch, database, tmp_path)
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
    execution_node.parameters = entry.parameters

    with pytest.raises(asyncio.CancelledError):
        await execution_node.run_node_as_process()

    if failure in {"shared", "database"}:
        assert killed == []
        assert proc.waited is False
        assert entry.status == TaskStatus.RUNNING
        assert entry.error is None
        if failure == "database":
            assert "could not record the child stop request" in caplog.text
    elif resource != "self":
        assert killed == []
        assert proc.waited is False
        assert entry.status == TaskStatus.TERMINATING
        assert entry.error == f"Terminated because parent task {parent_id} failed"
    else:
        assert killed == [4321]
        assert proc.waited is True
        expected_status = TaskStatus.TERMINATING if failure == "lookup" else TaskStatus.FAILED
        assert entry.status == expected_status
        assert entry.error == f"Terminated because parent task {parent_id} failed"


@pytest.mark.asyncio
async def test_parent_joining_during_cleanup_prevents_the_stop(monkeypatch, tmp_path):
    parent_id = ObjectId()
    child = _entry(parent_ids=[parent_id])
    database = _Database([child])
    _context(monkeypatch, database, tmp_path)
    update = database.collection.find_one_and_update

    async def join_before_claim(query, change):
        child.parent_ids.append(ObjectId())
        return await update(query, change)

    monkeypatch.setattr(database.collection, "find_one_and_update", join_before_claim)

    await fail_and_terminate_children(parent_id, runner_pid=os.getpid())

    assert child.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_set_status_failed_terminates_children(monkeypatch, tmp_path):
    parent_id = ObjectId()
    running = _entry(parent_ids=[parent_id], process_id=41)
    database = _Database([running])
    killed = []
    fake_context = _context(monkeypatch, database, tmp_path)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda pid: killed.append(pid) or True,
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
