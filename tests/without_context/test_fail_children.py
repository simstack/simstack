import asyncio
import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from simstack.models.document import ObjectId

from simstack.core.definitions import TaskStatus
from simstack.core.node import Node
from simstack.core.services import fail_children
from simstack.core.services import task_termination
from simstack.core.services.fail_children import (
    fail_and_terminate_children,
    fail_and_terminate_task,
)


class _Finished:
    def __init__(self, stdout=b"") -> None:
        self.returncode = 0
        self.stdout = stdout

    async def communicate(self):
        return self.stdout, b""


def test_process_children_use_portable_pid_parent_listing(monkeypatch):
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout="101 31\n202 42\n303 31\n")
    monkeypatch.setattr(task_termination.subprocess, "run", run)
    assert task_termination._child_pids(31) == [101, 303]
    assert calls == [["ps", "-axo", "pid=,ppid="]]


def test_process_tree_enumeration_failure_cannot_confirm_stop(monkeypatch):
    monkeypatch.setattr(task_termination.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(task_termination.TaskTerminationError, match="Could not list children"):
        task_termination._child_pids(31)


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


class _DomainClient:
    def __init__(self, entries) -> None:
        self.entries = list(entries)
        self.saved = []

    server_url = "https://example.invalid/api"

    async def get_task(self, task_id):
        return next((entry for entry in self.entries if entry.id == task_id), None)

    async def children(self, parent):
        return list(self.entries)

    def execution(self, entry):
        return parent_execution(entry.id)

    async def cancel_child(self, parent, execution, entry, reason):
        entry.status = TaskStatus.TERMINATING
        entry.error = entry.error or reason
        return entry

    async def termination_confirmed(self, entry, reason):
        entry.status = TaskStatus.FAILED
        entry.error = entry.error or reason
        entry.completed_at = datetime.now()
        return entry

    async def fail_attempt(self, entry, error, **kwargs):
        entry.status = TaskStatus.FAILED
        entry.error = entry.error or error
        entry.completed_at = datetime.now()
        return entry

    async def execution_control(self, entry):
        return {"container_started": False, "execution_stopped": False}


def parent_execution(task_id):
    return {"task_id": str(task_id), "generation": "parent-generation", "claim_id": "parent-claim", "start_id": "parent-start"}


def _entry(**overrides):
    values = {
        "id": ObjectId(),
        "repository_revision": None,
        "name": "partial",
        "status": TaskStatus.RUNNING,
        "job_id": None,
        "process_id": None,
        "error": None,
        "completed_at": None,
        "parameters": SimpleNamespace(resource="local", queue="default"),
        "_execution_claim_id": "original-claim",
        "_execution_generation": "original-generation",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(database, tmp_path):
    return SimpleNamespace(server=SimpleNamespace(tasks=database, slurm=database, requests=SimpleNamespace(server_url="https://example.invalid/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})), config=SimpleNamespace(workdir=tmp_path, resource="local"))


@pytest.mark.asyncio
async def test_remote_child_delegates_stop_without_local_pid_kill(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    parent = ObjectId()
    remote = _entry(process_id=99, parameters=SimpleNamespace(resource="other"), _execution_resource="other")
    database = _DomainClient([remote])
    remote.status = TaskStatus.TERMINATING
    database.cancel_child = AsyncMock(return_value=remote)
    active = _context(database,tmp_path)
    active.config.resource = "local"
    monkeypatch.setattr(fail_children,"context",active)
    monkeypatch.setattr(fail_children,"_stop_task",AsyncMock(side_effect=AssertionError("foreign PID killed locally")))
    await fail_and_terminate_task(remote,error="parent failed",runner_pid=os.getpid(),parent_id=parent, parent_execution=parent_execution(parent))
    database.cancel_child.assert_awaited_once_with(parent,parent_execution(parent),remote,"parent failed")
    fail_children._stop_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True], ids=["local-root", "local-child"])
async def test_local_actor_stops_only_its_owned_process_handle(monkeypatch, tmp_path, nested):
    entry = _entry(process_id=99, _execution_local=True, _execution_resource="other")
    database = _DomainClient([entry])
    proc = _CancelledProcess(4321)
    killed = []
    active = _context(database, tmp_path)
    active.server.requests.authorization = "local"
    monkeypatch.setattr(fail_children, "context", active)
    monkeypatch.setattr(task_termination, "_kill_process_tree", killed.append)
    parent = ObjectId() if nested else None

    assert await fail_and_terminate_task(
        entry, error="parent failed", runner_pid=os.getpid(), local_process=proc,
        parent_id=parent,
        parent_execution=parent_execution(parent) if nested else None,
    )

    assert killed == [4321]
    assert proc.waited
    assert entry.status == TaskStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("same_launch", [False, True], ids=["other-generation", "exact-launch"])
async def test_local_actor_uses_only_an_exact_registered_process_handle(
    monkeypatch, tmp_path, same_launch
):
    entry = _entry(process_id=99, _execution_local=True, _execution_start_id="original-start")
    registered = SimpleNamespace(**vars(entry))
    if not same_launch:
        registered._execution_generation = "another-generation"
    database = _DomainClient([entry])
    proc = _CancelledProcess(4321)
    killed = []
    active = _context(database, tmp_path)
    active.server.requests.authorization = "local"
    monkeypatch.setattr(fail_children, "context", active)
    monkeypatch.setattr(task_termination, "_kill_process_tree", killed.append)
    parent = ObjectId()

    with task_termination.track_local_process(registered, proc):
        assert await fail_and_terminate_task(
            entry, error="parent failed", runner_pid=os.getpid(),
            parent_id=parent, parent_execution=parent_execution(parent),
        )

    assert killed == ([4321] if same_launch else [])
    assert proc.waited is same_launch
    assert entry.status == (TaskStatus.FAILED if same_launch else TaskStatus.TERMINATING)
    assert task_termination.owned_local_process(registered) is None


@pytest.mark.asyncio
async def test_failed_parent_keeps_owned_child_terminating_when_stop_fails(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    parent = ObjectId()
    child = _entry(parameters=SimpleNamespace(resource="local"), _execution_resource="local")
    database = _DomainClient([child])
    child.status = TaskStatus.TERMINATING
    database.cancel_child = AsyncMock(return_value=child)
    active = _context(database, tmp_path)
    active.config.resource = "local"
    monkeypatch.setattr(fail_children, "context", active)
    monkeypatch.setattr(fail_children, "_stop_task", AsyncMock(side_effect=task_termination.TaskTerminationError("scancel denied")))
    with pytest.raises(task_termination.TaskTerminationError):
        await fail_and_terminate_task(child, error="parent failed", runner_pid=os.getpid(), parent_id=parent, parent_execution=parent_execution(parent))
    assert child.status == TaskStatus.TERMINATING
    database.cancel_child.assert_awaited_once_with(parent,parent_execution(parent),child,"parent failed")


@pytest.mark.asyncio
async def test_foreign_cached_missing_result_preserves_original_error(monkeypatch,tmp_path):
    from unittest.mock import AsyncMock
    from simstack.core import node as node_module
    from simstack.models import FloatData, NamedDataReference, NodeRegistry, Parameters
    from simstack.core.resources import allowed_resources
    monkeypatch.setattr(allowed_resources,"_initialized",False)
    monkeypatch.setattr(allowed_resources,"_resources",[])
    allowed_resources.set_resources(["local","other","self"])
    registry=NodeRegistry(name="cached",status=TaskStatus.COMPLETED,func_mapping="test.cached",function_hash="f",arg_hash="a",parameters=Parameters(resource="other"),results_references=[NamedDataReference(variable_name="value",variable_mapping=FloatData.__module__+".FloatData",reference=ObjectId())])
    object.__setattr__(registry,"_execution_resource","other")
    database=SimpleNamespace(resolve_task_outputs=AsyncMock(side_effect=LookupError("result not found")),fail_attempt=AsyncMock())
    active=SimpleNamespace(server=SimpleNamespace(tasks=SimpleNamespace(fail_attempt=database.fail_attempt), values=SimpleNamespace(resolve_task_outputs=database.resolve_task_outputs)),config=SimpleNamespace(resource="local",workdir=tmp_path))
    monkeypatch.setattr(node_module,"context",active)
    value = Node.__new__(Node)
    value.registry_entry = registry
    value.parameters = registry.parameters
    with pytest.raises(ValueError,match="result not found"):
        await value.load_results()
    database.resolve_task_outputs.assert_awaited_once_with(registry)
    database.fail_attempt.assert_not_called()
    assert registry.status==TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_parent_kills_running_children_and_leaves_completed(monkeypatch, tmp_path):
    parent_id = ObjectId()
    running = _entry(job_id="4321", process_id=99)
    completed = _entry(status=TaskStatus.COMPLETED, error=None)
    database = _DomainClient([running, completed])
    cidfile = tmp_path / running.name / str(running.id) / ".docker_cid"
    cidfile.parent.mkdir(parents=True)
    cidfile.write_text("abc123def\n", encoding="utf-8")
    commands = []
    killed = []

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return _Finished(stdout=b"4321|RUNNING\n" if len(commands) == 1 else b"")

    monkeypatch.setattr(fail_children, "context", _context(database, tmp_path))
    monkeypatch.setattr(task_termination.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )

    await fail_and_terminate_children(parent_id, runner_pid=os.getpid(), parent_execution=parent_execution(parent_id))

    query = ("squeue", "--noheader", "--me", "--all", "--local", "--states=all", "--format=%F|%T")
    assert commands == [query, ("scancel", "4321"), query]
    assert killed == []
    assert running.status == TaskStatus.FAILED
    assert running.error == f"Terminated because parent task {parent_id} failed"
    assert isinstance(running.completed_at, datetime)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.error is None


@pytest.mark.asyncio
async def test_loaded_string_parent_id_terminates_children(monkeypatch, tmp_path):
    parent_id = "6acc94fd41c9db0721eb8ac0"
    assert not isinstance(parent_id, ObjectId)
    running = _entry(process_id=41)
    database = _DomainClient([running])
    killed = []

    monkeypatch.setattr(fail_children, "context", _context(database, tmp_path))
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )

    await fail_and_terminate_children(parent_id, runner_pid=os.getpid(), parent_execution=parent_execution(parent_id))

    assert killed == [41]
    assert running.status == TaskStatus.FAILED
    assert running.error == f"Terminated because parent task {parent_id} failed"


@pytest.mark.asyncio
async def test_terminate_task_does_not_kill_the_runner(monkeypatch, tmp_path):
    runner_pid = os.getpid()
    entry = _entry(process_id=runner_pid)
    database = _DomainClient([entry])
    killed = []

    monkeypatch.setattr(fail_children, "context", _context(database, tmp_path))
    monkeypatch.setattr(
        task_termination.asyncio,
        "create_subprocess_exec",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("command ran")),
    )
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda process_id: killed.append(process_id)
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
@pytest.mark.parametrize("lookup_outage", [False, True])
async def test_cancelled_run_node_kills_and_fails_the_child(monkeypatch, tmp_path, lookup_outage):
    parent_id = ObjectId()
    entry = _entry()
    database = _DomainClient([entry])
    proc = _CancelledProcess(4321)
    killed = []
    children = database.children
    lookups = []
    async def observed_children(parent):
        lookups.append(parent)
        if lookup_outage and len(lookups) == 1:
            from simstack.server_api.http_transport import RunnerAPIError
            raise RunnerAPIError("Temporary stop lookup failure", status_code=503)
        return await children(parent)
    database.children = observed_children

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return proc

    monkeypatch.setattr(fail_children, "context", _context(database, tmp_path))
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda process_id: killed.append(process_id)
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
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(server_url="https://example.invalid/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
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
    execution_node._parent_execution = parent_execution(parent_id)
    execution_node.parameters = SimpleNamespace(resource="local")

    with pytest.raises(asyncio.CancelledError):
        await execution_node.run_node_as_process()

    assert killed == [4321]
    assert proc.waited is True
    if lookup_outage:
        assert len(lookups) > 1
    assert entry.status == TaskStatus.FAILED
    assert entry.error == f"Terminated because parent task {parent_id} failed"


@pytest.mark.asyncio
async def test_set_status_failed_terminates_children(monkeypatch, tmp_path):
    parent_id = ObjectId()
    running = _entry(process_id=41)
    database = _DomainClient([running])
    killed = []
    fake_context = _context(database, tmp_path)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    monkeypatch.setattr(fail_children, "context", fake_context)
    monkeypatch.setattr(
        task_termination, "_kill_process_tree", lambda process_id: killed.append(process_id)
    )
    node = Node.__new__(Node)
    node.name = "orchestrator"
    node.registry_entry = SimpleNamespace(
        id=parent_id,
        name="orchestrator",
        status=TaskStatus.RUNNING,
        error=None,
    )

    await node.set_status(TaskStatus.FAILED)

    assert node.registry_entry.status == TaskStatus.FAILED
    assert killed == [41]
    assert running.status == TaskStatus.FAILED
    assert running.error == f"Terminated because parent task {parent_id} failed"
