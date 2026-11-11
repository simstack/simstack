import os
import signal
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.models.document import ObjectId
from simstack.models.parameters import Resource
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


class _TaskAPI:
    def __init__(self, entry) -> None:
        self.entry = entry

    async def get_task(self, task_id):
        assert task_id == self.entry.id
        return self.entry

    async def children(self, entry):
        return []

    async def termination_confirmed(self, entry, error):
        assert entry is self.entry
        if entry.status != TaskStatus.TERMINATING:
            return None
        entry.status = TaskStatus.FAILED
        entry.error = error
        entry.completed_at = datetime.now()
        return entry


def _server(tasks):
    return SimpleNamespace(tasks=tasks, slurm=SimpleNamespace(execution_control=AsyncMock(
        return_value={"container_started": False, "execution_stopped": False})))


@pytest.fixture(autouse=True)
def no_descendant_tasks(monkeypatch):
    # Descendant cancellation has its own suite; these scheduler tasks are leaves.
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    monkeypatch.setattr("simstack.core.services.fail_children.context", SimpleNamespace(
        server=SimpleNamespace(tasks=SimpleNamespace(children=AsyncMock(return_value=[]), execution=WorkflowTasksAPI.execution))))


def _entry(**overrides):
    values = {
        "id": ObjectId(),
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
            server=_server(_TaskAPI(entry)),
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
            server=SimpleNamespace(tasks=_TaskAPI(entry)),
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
            server=SimpleNamespace(tasks=_TaskAPI(entry)),
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
            server=SimpleNamespace(tasks=_TaskAPI(entry)),
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
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(server=SimpleNamespace(tasks=_TaskAPI(entry)), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination.asyncio, "create_subprocess_exec", execute)
    monkeypatch.setattr(task_termination, "_kill_process_tree", lambda pid: pytest.fail("Slurm PID killed locally"))
    with pytest.raises(task_termination.TaskTerminationError):
        await terminate_task(entry, runner_pid=os.getpid())
    assert entry.status == TaskStatus.TERMINATING
    assert entry.completed_at is None


@pytest.mark.asyncio
async def test_slurm_without_job_id_never_falls_back_to_legacy_pid(monkeypatch, tmp_path):
    entry = _entry(process_id=99, parameters=SimpleNamespace(queue="slurm-queue"))
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(server=SimpleNamespace(tasks=_TaskAPI(entry)), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination, "_kill_process_tree", lambda pid: pytest.fail("Legacy PID killed locally"))
    with pytest.raises(task_termination.TaskTerminationError, match="valid job ID"):
        await terminate_task(entry, runner_pid=os.getpid())
    assert entry.status == TaskStatus.TERMINATING


@pytest.mark.asyncio
async def test_gone_slurm_job_is_not_confused_with_another_users_job_id(monkeypatch, tmp_path):
    entry = _entry(job_id="4321", process_id=99)
    query = AsyncMock(return_value="14321|RUNNING\n43210|RUNNING")
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(server=_server(_TaskAPI(entry)), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination, "_run_command", query)
    await terminate_task(entry, runner_pid=os.getpid())
    assert entry.status == TaskStatus.FAILED
    assert query.await_count == 1
    assert "--jobs" not in query.call_args.args[0]


@pytest.mark.asyncio
async def test_suspended_array_job_is_cancelled_and_retained_terminal_rows_are_not_active(monkeypatch, tmp_path):
    entry = _entry(job_id="4321", process_id=99)
    command = AsyncMock(side_effect=["4321|CANCELLED\n4321|SUSPENDED", "", "4321|CANCELLED"])
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(server=_server(_TaskAPI(entry)), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination, "_run_command", command)
    await terminate_task(entry, runner_pid=os.getpid())
    assert command.call_args_list[1].args[0] == ["scancel", "4321"]
    assert entry.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_api_cancellation_sends_observed_generation(monkeypatch, tmp_path):
    entry = _entry(job_id="4321", _execution_generation="old-attempt")
    tasks = _TaskAPI(entry)
    tasks.termination_confirmed = AsyncMock(return_value=None)
    monkeypatch.setattr(task_termination, "context", SimpleNamespace(server=_server(tasks), config=SimpleNamespace(workdir=tmp_path)))
    monkeypatch.setattr(task_termination, "_run_command", AsyncMock(return_value=""))
    await terminate_task(entry, runner_pid=os.getpid())
    assert tasks.termination_confirmed.await_args.args[0]._execution_generation == "old-attempt"
    assert entry.status == TaskStatus.TERMINATING


@pytest.mark.asyncio
async def test_stop_error_does_not_stop_runner_polling(monkeypatch):
    import asyncio
    from simstack.core.services import node_execution_service as module
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    entry = _entry()
    service = module.NodeExecutionService(Resource(value="test"), 1, 1, None)
    service._started = True
    service._claiming_paused = asyncio.Event()
    service._claiming_paused.set()
    tasks = SimpleNamespace(terminating_tasks=AsyncMock(return_value=[entry]), execution=WorkflowTasksAPI.execution)
    server = SimpleNamespace(tasks=tasks, resource=SimpleNamespace(active_containers=AsyncMock(return_value=[])), requests=SimpleNamespace(
        server_url="https://example.invalid/api", authorization="runner", resource="test",
        bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr(module, "context", SimpleNamespace(server=server))
    monkeypatch.setattr(module, "terminate_task", AsyncMock(side_effect=task_termination.TaskTerminationError("scancel denied")))
    await service.execute()
    module.terminate_task.assert_awaited_once()


def test_process_tree_stop_preserves_shared_subtree_on_unix(monkeypatch):
    children = {55: [7, 8], 7: [9], 8: [10]}
    killed = []
    monkeypatch.setattr(task_termination.platform, "system", lambda: "Linux")
    monkeypatch.setattr(task_termination, "_child_pids", lambda pid: children.get(pid, []))
    monkeypatch.setattr(task_termination.os, "kill", lambda pid, sig: killed.append(pid))
    _kill_process_tree(55, {7})
    assert killed == [10, 8, 55]


def test_process_tree_stop_preserves_shared_subtree_on_windows(monkeypatch):
    children = {55: [7, 8], 7: [9], 8: [10]}
    commands = []
    monkeypatch.setattr(task_termination.platform, "system", lambda: "Windows")
    monkeypatch.setattr(task_termination, "_child_pids", lambda pid: children.get(pid, []))
    monkeypatch.setattr(task_termination.subprocess, "run", lambda args, **kwargs: commands.append(args))
    _kill_process_tree(55, {7})
    assert commands == [["taskkill", "/PID", str(pid), "/F"] for pid in (10, 8, 55)]


def test_windows_child_lookup_reports_only_requested_parent_children(monkeypatch):
    commands = []
    monkeypatch.setattr(task_termination.platform, "system", lambda: "Windows")
    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout="101\r\n\r\n202\r\n")
    monkeypatch.setattr(task_termination.subprocess, "run", run)
    assert task_termination._child_pids(31) == [101, 202]
    assert "ParentProcessId -eq 31" in commands[0][-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["parent", "resource", "scheduler"])
async def test_shared_process_protection_does_not_trust_foreign_resource_pids(monkeypatch, foreign):
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    parent = _entry(_execution_generation="generation", _execution_claim_id="claim", _execution_start_id="start",
        _execution_resource="local")
    child = _entry(status=TaskStatus.RUNNING, process_id=1234, _execution_parent=WorkflowTasksAPI.execution(parent),
        _execution_launch_kind="inline", _execution_resource="local")
    if foreign == "parent":
        child._execution_parent = {**child._execution_parent, "claim_id": "another-owner"}
    elif foreign == "resource":
        child._execution_resource = "other"
    else:
        child._execution_launch_kind = "slurm"
    server = SimpleNamespace(tasks=SimpleNamespace(execution=WorkflowTasksAPI.execution))
    assert await task_termination.shared_launch_process(child, parent, server) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["account", "generation", "claim", "start", "parent"])
async def test_shared_prestart_process_receipt_requires_original_scope(monkeypatch, wrong):
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    parent = _entry(_execution_generation="parent-generation", _execution_claim_id="parent-claim", _execution_start_id="parent-start",
        _execution_resource="local")
    child = _entry(status=TaskStatus.RETRIEVED, process_id=None, _execution_parent=WorkflowTasksAPI.execution(parent),
        _execution_generation="child-generation", _execution_claim_id="child-claim", _execution_start_id="child-start",
        _execution_launch_kind="inline", _execution_resource="local", parameters=SimpleNamespace(in_docker=False))
    actor = {"server_url": "https://example.invalid/api", "account_id": "owner-account", "resource": "local", "authorization": "runner"}
    intent = {"actor": actor, "generation": "child-generation", "claim_id": "child-claim", "start_id": "child-start",
        "process_id": 1234, "publication": {"parent_execution": WorkflowTasksAPI.execution(parent)}}
    if wrong == "account":
        intent["actor"] = {**actor, "account_id": "other-account"}
    elif wrong == "generation":
        intent["generation"] = "other-generation"
    elif wrong == "claim":
        intent["claim_id"] = "other-claim"
    elif wrong == "start":
        intent["start_id"] = "other-start"
    else:
        intent["publication"] = {"parent_execution": {**WorkflowTasksAPI.execution(parent), "start_id": "other-parent-start"}}
    server = SimpleNamespace(tasks=SimpleNamespace(execution=WorkflowTasksAPI.execution),
        requests=SimpleNamespace(server_url=actor["server_url"], resource="local", authorization="runner",
            bootstrap_data={"account_id": "owner-account"}))
    monkeypatch.setattr("simstack.runner_runtime.launch_claims.receipt", lambda *args: intent)
    with pytest.raises(task_termination.TaskTerminationError, match="original shared child launch"):
        await task_termination.shared_launch_process(child, parent, server)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested_launch", [False, True], ids=["launched-by-root", "launched-by-middle"])
@pytest.mark.parametrize("external_consumer", [False, True], ids=["internal-consumers-only", "external-consumer"])
@pytest.mark.parametrize("reverse_children", [False, True], ids=["forward-order", "reverse-order"])
async def test_dag_cancellation_detaches_each_consumer_before_protecting_original_launch(
    monkeypatch, nested_launch, external_consumer, reverse_children,
):
    from copy import deepcopy
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI

    def task(pid, parents=(), original_parent=None):
        return _entry(status=TaskStatus.RUNNING, process_id=pid,
            parent_ids=[parent.id for parent in parents], parameters=SimpleNamespace(in_docker=False),
            _execution_generation="generation", _execution_claim_id=str(pid), _execution_start_id=str(pid),
            _execution_launch_kind="inline", _execution_resource="local",
            _execution_parent=WorkflowTasksAPI.execution(original_parent) if original_parent else None)

    root = task(101)
    root.status = TaskStatus.TERMINATING
    middle = task(202, [root], root)
    other = task(404)
    child = task(303, [root, middle, *([other] if external_consumer else [])],
        middle if nested_launch else root)
    rows = [root, middle, child, other]
    detached, cancellations = {row.id: set() for row in rows}, []
    active = {TaskStatus.SUBMITTED, TaskStatus.RETRIEVED, TaskStatus.RUNNING,
              TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING}

    class Tasks:
        execution = staticmethod(WorkflowTasksAPI.execution)

        async def children(self, parent):
            found = [deepcopy(row) for row in rows if parent.id in row.parent_ids]
            return found[::-1] if reverse_children else found

        async def cancel_child(self, parent_id, execution, entry, reason):
            row = next(row for row in rows if row.id == entry.id)
            cancellations.append((parent_id, row.id))
            detached[row.id].add(parent_id)
            consumers = [parent for parent in rows if parent.id in row.parent_ids
                and parent.id not in detached[row.id] and parent.status in active]
            if not consumers and row.status in active:
                row.status = TaskStatus.TERMINATING
            return deepcopy(row)

    server = SimpleNamespace(tasks=Tasks(), requests=SimpleNamespace(
        server_url="https://example.invalid/api", authorization="runner", resource="local",
        bootstrap_data={"account_id": "test-account"}))
    protected = await task_termination.protected_child_processes(root, server)
    killed = []
    physical = {101: [202], 202: [303]} if nested_launch else {101: [202, 303]}
    monkeypatch.setattr(task_termination.platform, "system", lambda: "Linux")
    monkeypatch.setattr(task_termination, "_child_pids", lambda pid: physical.get(pid, []))
    monkeypatch.setattr(task_termination.os, "kill", lambda pid, sig: killed.append(pid))
    task_termination._kill_process_tree(root.process_id, protected)

    assert cancellations.count((root.id, child.id)) == 1
    assert cancellations.count((middle.id, child.id)) == 1
    assert protected == ({child.process_id} if external_consumer else set())
    assert child.status == (TaskStatus.RUNNING if external_consumer else TaskStatus.TERMINATING)
    assert set(killed) == ({101, 202} if external_consumer else {101, 202, 303})


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process sessions")
async def test_dead_controller_cannot_acknowledge_a_live_scientific_worker(monkeypatch, tmp_path):
    import asyncio
    import sys
    import subprocess
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    worker_pid = tmp_path / "worker-pid"
    controller = await asyncio.create_subprocess_exec(sys.executable, "-c",
        "import os, subprocess, sys, time; from pathlib import Path; "
        "worker=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"Path({str(worker_pid)!r}).write_text(str(worker.pid)); os.kill(os.getpid(), 9)",
        start_new_session=True)
    controller._simstack_process_group = True
    await controller.wait()
    pid = int(worker_pid.read_text())
    entry = _entry(parameters=SimpleNamespace(in_docker=False, queue="default"),
        _execution_local=True, _execution_generation="generation", _execution_claim_id="claim", _execution_start_id="start")
    entry._execution_owner = {"process_id": controller.pid, "execution": WorkflowTasksAPI.execution(entry)}
    api = SimpleNamespace(tasks=SimpleNamespace(children=AsyncMock(return_value=[]), execution=WorkflowTasksAPI.execution))
    try:
        os.kill(pid, 0)  # Killing the controller really left its worker running.
        await asyncio.wait_for(task_termination.stop_task_execution(entry, server=api,
            runner_pid=os.getpid(), workdir=tmp_path, local_process=controller), 3)
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        assert not result.stdout.strip() or result.stdout.strip().startswith("Z")
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process sessions")
async def test_unresponsive_owned_controller_does_not_block_physical_stop(tmp_path):
    import asyncio
    import sys
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True)
    process._simstack_process_group = True
    entry = _entry(parameters=SimpleNamespace(in_docker=False, queue="default"),
        _execution_local=True, _execution_generation="generation", _execution_claim_id="claim", _execution_start_id="start")
    entry._execution_owner = {"process_id": process.pid, "execution": WorkflowTasksAPI.execution(entry)}
    api = SimpleNamespace(tasks=SimpleNamespace(children=AsyncMock(return_value=[]), execution=WorkflowTasksAPI.execution))
    try:
        await asyncio.wait_for(task_termination.stop_task_execution(entry, server=api,
            runner_pid=os.getpid(), workdir=tmp_path, local_process=process), 3)
        assert process.returncode is not None
        api.tasks.children.assert_awaited_once()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
