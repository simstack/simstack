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
    def __init__(self, returncode: int = 0, stdout=b"", stderr=b"") -> None:
        self.returncode = returncode
        self.stdout, self.stderr = stdout, stderr

    async def communicate(self):
        return self.stdout, self.stderr


class _Database:
    def __init__(self, entry) -> None:
        self.entry = entry

    async def load_task_by_id(self, task_id):
        assert task_id == self.entry.id
        return self.entry

    async def task_mutation(self, task_id, action, values=None, expected_statuses=None):
        assert task_id == self.entry.id
        assert action == "terminate"
        assert expected_statuses == [TaskStatus.TERMINATING.value]
        if self.entry.status.value not in expected_statuses:
            return None
        assert set(values) == {"status", "error", "completed_at"}
        self.entry.status = TaskStatus(values["status"])
        self.entry.error = values["error"]
        self.entry.completed_at = values["completed_at"]
        return self.entry


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
async def test_slurm_termination_never_kills_login_process_or_container(
    monkeypatch, tmp_path
):
    entry = _entry(job_id="4321", process_id=99)
    cidfile = tmp_path / entry.name / str(entry.id) / ".docker_cid"
    cidfile.parent.mkdir(parents=True)
    cidfile.write_text("abc123def\n", encoding="utf-8")
    commands = []

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return _Finished(stdout=b"4321|RUNNING\n" if len(commands) == 1 else b"")

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

    query = ("squeue", "--noheader", "--me", "--all", "--local", "--states=all", "--format=%F|%T")
    assert commands == [query, ("scancel", "4321"), query]
    assert killed == []
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["scancel", "squeue", "still_running"])
async def test_slurm_stop_failure_does_not_report_task_stopped(monkeypatch, tmp_path, failure):
    entry = _entry(job_id="4321", process_id=99)
    results = [_Finished(stdout=b"4321|RUNNING\n")]
    results += [_Finished(returncode=1, stderr=b"cancel denied")] if failure == "scancel" else [
        _Finished(), _Finished(returncode=1, stderr=b"scheduler unavailable") if failure == "squeue"
        else _Finished(stdout=b"4321|RUNNING\n")]
    async def execute(*args, **kwargs):
        return results.pop(0)
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(db=_Database(entry), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination.asyncio, "create_subprocess_exec", execute)
    monkeypatch.setattr(task_termination, "_kill_process_tree", lambda pid: pytest.fail("Slurm PID killed locally"))
    with pytest.raises(task_termination.TaskTerminationError):
        await terminate_task(entry, runner_pid=os.getpid())
    assert entry.status == TaskStatus.TERMINATING
    assert entry.completed_at is None


@pytest.mark.asyncio
async def test_slurm_without_job_id_never_falls_back_to_legacy_pid(monkeypatch, tmp_path):
    entry = _entry(process_id=99, parameters=SimpleNamespace(queue="slurm-queue"))
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(db=_Database(entry), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination, "_kill_process_tree", lambda pid: pytest.fail("Legacy PID killed locally"))
    with pytest.raises(task_termination.TaskTerminationError, match="valid job ID"):
        await terminate_task(entry, runner_pid=os.getpid())
    assert entry.status == TaskStatus.TERMINATING


@pytest.mark.asyncio
async def test_gone_slurm_job_is_not_confused_with_another_users_job_id(monkeypatch, tmp_path):
    entry = _entry(job_id="4321", process_id=99)
    query = AsyncMock(return_value="14321|RUNNING\n43210|RUNNING")
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(db=_Database(entry), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination, "_run_command", query)
    await terminate_task(entry, runner_pid=os.getpid())
    assert entry.status == TaskStatus.FAILED
    assert query.await_count == 1
    assert "--jobs" not in query.call_args.args[0]


@pytest.mark.asyncio
async def test_suspended_array_job_is_cancelled_and_retained_terminal_rows_are_not_active(monkeypatch, tmp_path):
    entry = _entry(job_id="4321", process_id=99)
    command = AsyncMock(side_effect=["4321|CANCELLED\n4321|SUSPENDED", "", "4321|CANCELLED"])
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(db=_Database(entry), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination, "_run_command", command)
    await terminate_task(entry, runner_pid=os.getpid())
    assert command.call_args_list[1].args[0] == ["scancel", "4321"]
    assert entry.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_api_cancellation_sends_observed_generation(monkeypatch, tmp_path):
    from simstack.core.definitions import DBType
    entry = _entry(job_id="4321", _execution_generation="old-attempt")
    database = _Database(entry)
    database.databae_type = DBType.API
    database.task_mutation = AsyncMock(return_value=None)
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(db=database, config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination, "_run_command", AsyncMock(return_value=""))
    await terminate_task(entry, runner_pid=os.getpid())
    assert database.task_mutation.call_args.args[2]["generation"] == "old-attempt"
    assert entry.status == TaskStatus.TERMINATING


@pytest.mark.asyncio
async def test_stop_error_does_not_stop_runner_polling(monkeypatch):
    import asyncio
    from simstack.core.services import node_execution_service as module
    entry = _entry()
    service = module.NodeExecutionService.__new__(module.NodeExecutionService)
    service._started, service._running_tasks = True, set()
    service._resource_name, service._pid = "test", os.getpid()
    service._claiming_paused = asyncio.Event()
    service._claiming_paused.set()
    database = SimpleNamespace(load_terminating_tasks_for_resource=AsyncMock(return_value=[entry]))
    monkeypatch.setattr(module, "context", SimpleNamespace(db=database))
    monkeypatch.setattr(module, "terminate_task", AsyncMock(side_effect=task_termination.TaskTerminationError("scancel denied")))
    await service.execute()
    module.terminate_task.assert_awaited_once()
