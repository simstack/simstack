"""Regression coverage for scheduler evidence and independently owned science."""
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.services import task_termination
from simstack.util import slurm_observation


@pytest.mark.asyncio
async def test_cancel_query_ignores_user_scheduler_filters(monkeypatch):
    monkeypatch.setenv("SQUEUE_PARTITION", "unrelated")
    monkeypatch.setenv("SQUEUE_QOS", "hidden")
    environment = {}
    async def spawn(*command, **kwargs):
        environment.update(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"42|RUNNING\n", b"")))
    monkeypatch.setattr(task_termination.asyncio, "create_subprocess_exec", spawn)
    assert await task_termination._slurm_job_present("42")
    assert not any(key.startswith("SQUEUE_") for key in environment)


@pytest.mark.parametrize("state", ["SPECIAL_EXIT", "RESV_DEL_HOLD", "REQUEUE_HOLD"])
def test_held_allocation_cannot_be_finished_by_previous_accounting(monkeypatch, state):
    queries = []
    def run(command):
        queries.append(command)
        if command[0] == "squeue":
            return f"42|{state}|SE|calculation|user|0:00|node01|Held\n"
        return "42|FAILED|17:0|2026-11-10T12:00:00|Previous incarnation\n"
    monkeypatch.setattr(slurm_observation, "_run", run)
    result = slurm_observation.observe_job("42")
    assert result["kind"] == "active" and result["state"] == state
    assert len(queries) == 1


def test_accounting_uses_unambiguous_utc_time_format(monkeypatch):
    monkeypatch.setenv("SLURM_TIME_FORMAT", "relative")
    def run(command, **kwargs):
        environment = kwargs["env"]
        assert environment["SLURM_TIME_FORMAT"] == "%Y-%m-%dT%H:%M:%S"
        assert environment["TZ"] == "UTC"
        return SimpleNamespace(returncode=0, stdout="" if command[0] == "squeue" else
            "42|OUT_OF_MEMORY|0:9|2026-11-10T12:00:00|Memory limit\n")
    monkeypatch.setattr(slurm_observation.subprocess, "run", run)
    result = slurm_observation.observe_job("42")
    assert result["kind"] == "terminal" and result["state"] == "OUT_OF_MEMORY"


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_queue_absence_requires_exact_docker_physical_ack(monkeypatch, stopped, tmp_path):
    entry = SimpleNamespace(job_id="42", parameters=SimpleNamespace(queue="slurm-queue", in_docker=True))
    server = SimpleNamespace(slurm=SimpleNamespace(execution_control=AsyncMock(return_value={
        "container_started": True, "execution_stopped": stopped})))
    monkeypatch.setattr(task_termination, "_slurm_job_present", AsyncMock(return_value=False))
    if stopped:
        await task_termination.stop_task_execution(entry, server=server, runner_pid=os.getpid(), workdir=tmp_path)
    else:
        with pytest.raises(task_termination.TaskTerminationError, match="physical stop"):
            await task_termination.stop_task_execution(entry, server=server, runner_pid=os.getpid(), workdir=tmp_path)


@pytest.mark.asyncio
async def test_unowned_controller_fallback_accepts_timeout_enum(tmp_path):
    process = SimpleNamespace(pid=42, returncode=0, wait=AsyncMock())
    entry = SimpleNamespace(_execution_owner={"process_id": 42}, parameters=SimpleNamespace(in_docker=False))
    server = SimpleNamespace(tasks=SimpleNamespace(get_outcome=AsyncMock(return_value=SimpleNamespace(status=TaskStatus.TIME_OUT))))
    await task_termination.stop_task_execution(entry, server=server, runner_pid=os.getpid(), workdir=tmp_path, local_process=process)
    process.wait.assert_awaited_once()


def process_running(pid):
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() and not result.stdout.strip().startswith("Z")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX controller/session integration")
@pytest.mark.parametrize("controller_dies,in_container", [(False, False), (True, False), (False, True)])
async def test_real_supervisor_survives_runner_death_and_drains_science(tmp_path, controller_dies, in_container):
    """The real monitor retains cancellation independently of the old Runner."""
    if in_container and not os.environ.get("SIMSTACK_TEST_DOCKER_IMAGE"):
        pytest.skip("SIMSTACK_TEST_DOCKER_IMAGE with Core dependencies required")
    worker = '''
import json,os,signal,subprocess,sys,time
from pathlib import Path
from simstack.core.run_node import _guard_controller_lifetime
_guard_controller_lifetime(int(os.environ["SIMSTACK_SCIENCE_CONTROLLER_PID"]))
child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])
signal.signal(signal.SIGUSR1,lambda *_:Path(os.environ["AUDIT_ROOT"],"checkpoint").write_text("received"))
Path(os.environ["AUDIT_ROOT"],"science.json").write_text(json.dumps({"worker":os.getpid(),"child":child.pid}))
time.sleep(60)
'''
    controller = '''
import asyncio,json,os,sys
from pathlib import Path
from types import SimpleNamespace
from simstack.core import run_node
from simstack.core.definitions import TaskStatus
from simstack.core.run_node_protocol import RunNodeResult
from simstack.runner_runtime import execution_environment
from simstack.server_api.workflow_tasks import WorkflowTasksAPI
root=Path(os.environ["AUDIT_ROOT"])
entry=SimpleNamespace(id="123456789012345678901234",repository_revision=None,
 status=TaskStatus.RUNNING,error=None,job_id=None,parameters=SimpleNamespace(in_docker=False),
 _execution_generation="generation",_execution_claim_id="claim",_execution_start_id="start")
async def outcome(_):
 return SimpleNamespace(**{**vars(entry),"status":TaskStatus.TERMINATING if (root/"cancel").exists() else TaskStatus.RUNNING})
async def confirmed(*args):
 (root/"ack").write_text("stopped")
 return SimpleNamespace(**{**vars(entry),"status":TaskStatus.FAILED,"error":"cancelled","return_kind":"exception"})
async def control(_):
 return {"stop_requested":(root/"cancel").exists(),"reason":"allocation cancelled"}
async def intent(_):return {}
server=SimpleNamespace(slurm=SimpleNamespace(execution_control=control,container_intent=intent),tasks=SimpleNamespace(execution=WorkflowTasksAPI.execution,get_outcome=outcome,termination_confirmed=confirmed,children=lambda _:[]),
 requests=SimpleNamespace(server_url="https://supervisor.invalid/api",resource="test",authorization="runner",bootstrap_data={"account_id":"test"}))
run_node.context=SimpleNamespace(server=server,config=SimpleNamespace(project_root=root,workdir=root))
execution_environment.task_environment=lambda *args,**kwargs:dict(os.environ)
execution_environment.task_launch_command=lambda *args,**kwargs:[sys.executable,"-c",WORKER]
async def no_children(*args):return set()
from simstack.core.services import task_termination
task_termination.protected_child_processes=no_children
asyncio.run(run_node._run_local_controller(entry,"test",root,in_docker=os.environ.get("IN_CONTAINER")=="1"))
'''.replace('WORKER', repr(worker))
    launcher = '''
import os,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,"-c",CONTROLLER],start_new_session=True)
Path(os.environ["AUDIT_ROOT"],"controller").write_text(str(child.pid))
time.sleep(60)
'''.replace('CONTROLLER',repr(controller))
    log = (tmp_path / "process.log").open("wb")
    container_name = None
    if in_container:
        from uuid import uuid4
        container_name = "simstack-fix-supervisor-" + uuid4().hex
        core = Path(__file__).resolve().parents[2] / "src"
        command = ["docker", "run", "--name", container_name, "--user", "0:0", "--memory", "512m",
            "-v", f"{core}:/core:ro", "-v", f"{tmp_path}:/data", "-e", "PYTHONPATH=/core",
            "-e", "AUDIT_ROOT=/data", "-e", "IN_CONTAINER=1", "-e", "SIMSTACK_SLURM_CONTAINER_EXECUTION=1",
            "--entrypoint", "/home/simstack/.venv/bin/python", os.environ["SIMSTACK_TEST_DOCKER_IMAGE"], "-c", controller]
    else:
        command = [sys.executable,"-c",launcher]
    parent = subprocess.Popen(command, env={**os.environ,"AUDIT_ROOT":str(tmp_path)},stdout=log,stderr=log)
    ids = None; supervisor = None
    try:
        for _ in range(500):
            if (tmp_path/"science.json").exists():break
            await asyncio.sleep(.01)
        assert (tmp_path/"science.json").exists(), (tmp_path/"process.log").read_text()
        ids=json.loads((tmp_path/"science.json").read_text())
        if not in_container:supervisor=int((tmp_path/"controller").read_text())
        if in_container:
            sent=subprocess.run(["docker","kill","--signal","USR1",container_name],capture_output=True,text=True)
            assert sent.returncode==0, sent.stderr
            for _ in range(200):
                if (tmp_path/"checkpoint").exists():break
                await asyncio.sleep(.01)
            assert (tmp_path/"checkpoint").exists(), (tmp_path/"process.log").read_text()
        parent.kill();await asyncio.to_thread(parent.wait,5)
        def science_alive():
            if in_container:
                result=subprocess.run(["docker","inspect","--format","{{.State.Running}}",container_name],capture_output=True,text=True)
                return result.stdout.strip()=="true"
            return any(process_running(pid) for pid in ids.values())
        assert science_alive()
        if controller_dies:os.kill(supervisor,signal.SIGKILL)
        else:(tmp_path/"cancel").write_text("cancel")
        for _ in range(500):
            if not science_alive() and (controller_dies or (tmp_path/"ack").exists()):break
            await asyncio.sleep(.01)
        assert not science_alive(), (tmp_path/"process.log").read_text()
        if not controller_dies:assert (tmp_path/"ack").exists()
    finally:
        if parent.poll() is None:parent.kill()
        await asyncio.to_thread(parent.wait)
        if supervisor:
            try:os.killpg(supervisor,signal.SIGKILL)
            except ProcessLookupError:pass
        if container_name:
            subprocess.run(["docker","rm","-f",container_name],capture_output=True,check=False)
        log.close()


@pytest.mark.asyncio
async def test_foreground_finally_cannot_release_next_incarnations_capacity():
    from simstack.core.services.node_execution_service import NodeExecutionService
    service = NodeExecutionService("local", 1, 1, None)
    original, replacement = SimpleNamespace(id="same-task"), SimpleNamespace(id="same-task")
    await service._semaphore.acquire()
    service._launch_slots.add(id(original))
    service._active_ids.add(original.id)
    service._release_launch_slot(original)
    await service._semaphore.acquire()
    service._launch_slots.add(id(replacement))
    service._active_ids.add(replacement.id)
    service._release_launch_slot(original)
    assert service._semaphore.locked() and replacement.id in service._active_ids
    service._release_launch_slot(replacement)
    assert not service._semaphore.locked() and not service._active_ids


@pytest.mark.asyncio
async def test_cancel_before_launch_capacity_releases_reserved_claim():
    from unittest.mock import Mock
    from simstack.core.services.node_execution_service import NodeExecutionService
    service = NodeExecutionService("local", 1, 1, None)
    reservation = Mock()
    entry = SimpleNamespace(id="waiting-task", _launch_reservation=reservation)
    service._active_ids.add(entry.id)
    await service._semaphore.acquire()
    waiting = asyncio.create_task(service._run_with_semaphore(entry))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    reservation.close.assert_called_once()
    assert entry._launch_reservation is None
    assert entry.id not in service._active_ids and service._semaphore.locked()


@pytest.mark.asyncio
async def test_apptainer_allocation_stop_uses_scheduler_evidence_without_daemon_ack(monkeypatch, tmp_path):
    entry = SimpleNamespace(job_id="42", parameters=SimpleNamespace(queue="slurm-queue", in_docker=True))
    server = SimpleNamespace(slurm=SimpleNamespace(execution_control=AsyncMock(return_value={
        "container_started": True, "container_backend": "apptainer", "execution_stopped": False})))
    monkeypatch.setattr(task_termination, "_slurm_job_present", AsyncMock(return_value=False))
    await task_termination.stop_task_execution(entry, server=server, runner_pid=os.getpid(), workdir=tmp_path)


@pytest.mark.asyncio
async def test_default_apptainer_stop_defers_to_supervisor_instead_of_trusting_pid(tmp_path):
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    entry = SimpleNamespace(id="123456789012345678901234", name="science", job_id=None,
        process_id=os.getpid() + 1000, _execution_container_backend="apptainer",
        _execution_generation="generation", _execution_claim_id="claim", _execution_start_id="start",
        parameters=SimpleNamespace(queue="default", in_docker=True))
    server = SimpleNamespace(tasks=SimpleNamespace(execution=WorkflowTasksAPI.execution))
    with pytest.raises(task_termination.TaskTerminationError, match="owning supervisor"):
        await task_termination.stop_task_execution(entry, server=server, runner_pid=os.getpid(), workdir=tmp_path)


@pytest.mark.parametrize("error", ["Error: No such object: task", "error: no such object: task",
    "Error response from daemon: No such container: task"])
def test_docker_absence_accepts_cli_case_without_hiding_daemon_errors(monkeypatch, error):
    from simstack.core.run_docker import inspect_docker_execution
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr=error))
    assert inspect_docker_execution("task") is None
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1,
        stderr="Cannot connect to the Docker daemon"))
    with pytest.raises(RuntimeError, match="observation is unavailable"):
        inspect_docker_execution("task")


@pytest.mark.asyncio
async def test_foreground_docker_parent_releases_capacity_before_its_child_finishes(monkeypatch, tmp_path):
    from simstack.core.services import node_execution_service
    from simstack.runner_runtime import execution_environment
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI

    def entry(identity):
        return SimpleNamespace(id=identity * 24, name="science", repository_revision=None,
            _execution_generation="generation", _execution_claim_id=identity + "claim",
            _execution_start_id=identity + "start", status=TaskStatus.RETRIEVED,
            parameters=SimpleNamespace(queue="default", in_docker=True))
    parent, child = entry("a"), entry("b")
    started = set()
    child_finished = asyncio.Event()
    async def docker(task, *, on_observed_process=None, retry_unpersisted_evidence=False):
        assert callable(on_observed_process)
        assert retry_unpersisted_evidence is False
        started.add(task.id)
        if task.id == parent.id:
            await child_finished.wait()
        else:
            child_finished.set()
        return True
    async def get_task(identity):
        task = parent if identity == parent.id else child
        return SimpleNamespace(status=TaskStatus.RUNNING if identity in started else TaskStatus.RETRIEVED,
            _execution_start_id=task._execution_start_id if identity in started else None)
    server = SimpleNamespace(tasks=SimpleNamespace(get_task=get_task, execution=WorkflowTasksAPI.execution),
        requests=SimpleNamespace(resource="local", bootstrap_data={"account_id": "test"},
            server_url="http://runner.test", authorization="runner"))
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr(node_execution_service, "context", SimpleNamespace(server=server,
        config=SimpleNamespace(resource="local", workdir=tmp_path)))
    monkeypatch.setattr(execution_environment, "prepare_task", AsyncMock())
    monkeypatch.setattr(node_execution_service, "run_docker", docker)
    service = node_execution_service.NodeExecutionService("local", 1, 1, None, detach=False)
    service.write_node_event = AsyncMock()
    service._active_ids.update((parent.id, child.id))
    parent_run = asyncio.create_task(service._run_with_semaphore(parent))
    child_run = asyncio.create_task(service._run_with_semaphore(child))
    try:
        assert await asyncio.wait_for(asyncio.gather(parent_run, child_run), 3) == [True, True]
        assert child_finished.is_set() and not service._active_ids
    finally:
        for task in (parent_run, child_run, *service._observers):
            if not task.done():
                task.cancel()
        await asyncio.gather(parent_run, child_run, *service._observers, return_exceptions=True)
