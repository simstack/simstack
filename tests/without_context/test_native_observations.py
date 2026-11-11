"""Real inherited lifetime leases across launcher death, with isolated API state."""
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

from simstack.runner_runtime import native_observations as observations
from simstack.runner_runtime.launch_claims import _reserve_path
from simstack.server_api.http_transport import RunnerAPIError
from simstack.server_api.workflow_tasks import WorkflowTasksAPI


def execution_entry():
    return SimpleNamespace(id="123456789012345678901234", _execution_generation="generation",
        _execution_claim_id="claim", _execution_start_id="start", status="running", error=None)


def api_client(entry=None):
    entry = entry or execution_entry()
    execution = WorkflowTasksAPI.execution(entry)
    async def reconcile(*, claims, outcomes, **_):
        if claims:
            return {"claims": [{"execution": execution, "state": "active", "task": entry}]}
        return {"outcomes": [{"execution": execution, "state": "recorded"}]}
    return SimpleNamespace(requests=SimpleNamespace(server_url="https://observer.invalid/api",
        resource="test", authorization="runner", bootstrap_data={"account_id": "account"}),
        tasks=SimpleNamespace(execution=WorkflowTasksAPI.execution,
            reconcile_attempts=AsyncMock(side_effect=reconcile), termination_confirmed=AsyncMock()))


async def wait_file(path, parent):
    for _ in range(500):
        if path.exists():
            return json.loads(path.read_text())
        if parent.poll() is not None:
            raise AssertionError(parent.communicate())
        await asyncio.sleep(.01)
    raise AssertionError(f"Timed out waiting for {path.name}")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL/process-group integration")
@pytest.mark.parametrize("descendant", [False, True])
async def test_real_worker_lease_survives_parent_death_and_requires_empty_group(tmp_path, monkeypatch, descendant):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    # A separate launcher prepares durable ownership, then a real child adopts
    # it before the simulated start ACK. Neither handle survives in this test.
    setup = '''
import json, os, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
from simstack.runner_runtime import native_observations as observations
from simstack.server_api.workflow_tasks import WorkflowTasksAPI
root = Path(os.environ["SIMSTACK_RUNNER_WORKDIR"])
entry = SimpleNamespace(id="123456789012345678901234", _execution_generation="generation",
    _execution_claim_id="claim", _execution_start_id="start")
server = SimpleNamespace(requests=SimpleNamespace(server_url="https://observer.invalid/api",
    resource="test", authorization="runner", bootstrap_data={"account_id": "account"}),
    tasks=SimpleNamespace(execution=WorkflowTasksAPI.execution))
'''
    worker = setup + '''
observations.adopt(server, entry)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) if DESCENDANT else None
(root / "started").write_text(json.dumps({"pid": os.getpid(), "child": child.pid if child else None}))
time.sleep(60)
'''.replace("DESCENDANT", repr(descendant))
    launcher = setup + '''
value, lease = observations.prepare(server, entry)
environment = dict(os.environ, SIMSTACK_NATIVE_OBSERVATION_ID=value["observation_id"],
    SIMSTACK_NATIVE_OBSERVATION_EXECUTION=json.dumps(value["execution"]))
with lease, observations.inherit(lease, {}) as ownership:
    child = subprocess.Popen([sys.executable, "-c", WORKER], env=environment,
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **ownership)
# Deliberately never call spawned(): worker adoption must close that crash window.
time.sleep(60)
'''.replace("WORKER", repr(worker))
    parent = subprocess.Popen([sys.executable, "-c", launcher], env=dict(os.environ),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = None
    try:
        started = await wait_file(tmp_path / "started", parent)
        parent.kill()
        await asyncio.to_thread(parent.wait, 5)
        server = api_client()  # A fresh Runner has no original process handles.
        path, = observations._root(server).glob("*.json")
        value = json.loads(path.read_text())
        assert value["process_id"] == started["pid"]
        assert _reserve_path(path.with_suffix(".lease")) is None
        await observations.recover(server)
        server.tasks.reconcile_attempts.assert_not_awaited()
        assert path.exists()

        os.kill(started["pid"], signal.SIGKILL)
        for _ in range(200):
            probe = _reserve_path(path.with_suffix(".lease"))
            if probe is not None:
                probe.close()
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail("Original worker retained its lease after SIGKILL")
        if descendant:
            await observations.recover(server)
            assert path.exists()
            assert all(not call.kwargs["outcomes"] for call in server.tasks.reconcile_attempts.await_args_list)
            server.tasks.termination_confirmed.assert_not_awaited()
            os.kill(started["child"], signal.SIGKILL)
        for _ in range(200):
            if observations._science_stopped(value):
                break
            await asyncio.sleep(.01)
        await observations.recover(server)
        assert not path.exists()
        outcomes = [call.kwargs["outcomes"][0] for call in server.tasks.reconcile_attempts.await_args_list
                    if call.kwargs["outcomes"]]
        assert len(outcomes) == 1 and outcomes[0]["execution"] == WorkflowTasksAPI.execution(execution_entry())
        assert outcomes[0]["exit_code"] is None and outcomes[0]["signal"] is None
    finally:
        if parent.poll() is None:
            parent.kill()
        await asyncio.to_thread(parent.communicate)
        if started:
            for pid in (started["pid"], started["child"]):
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, 403, 409, 503])
async def test_recovery_retains_evidence_through_api_outage(tmp_path, monkeypatch, status):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server, entry = api_client(), execution_entry()
    value, lease = observations.prepare(server, entry)
    lease.close()
    monkeypatch.setattr(observations, "_science_stopped", lambda _: True)
    normal = server.tasks.reconcile_attempts.side_effect
    server.tasks.reconcile_attempts.side_effect = RunnerAPIError("unavailable", status_code=status)
    await observations.recover(server)
    path = observations._root(server) / (value["observation_id"] + ".json")
    assert path.exists()
    server.tasks.reconcile_attempts.side_effect = normal
    await observations.recover(server)
    assert not path.exists()


@pytest.mark.asyncio
async def test_unstarted_claim_is_recovered_instead_of_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server, entry = api_client(), execution_entry()
    value, lease = observations.prepare(server, entry)
    lease.close()  # Launcher died before spawning.
    server.tasks.reconcile_attempts.side_effect = None
    server.tasks.reconcile_attempts.return_value = {"claims": [
        {"execution": value["execution"], "state": "claimed", "task": entry}]}
    await observations.recover(server)
    assert not list(observations._root(server).glob("*.json"))
    assert server.tasks.reconcile_attempts.await_count == 1
    assert server.tasks.reconcile_attempts.await_args.kwargs["outcomes"] == []


@pytest.mark.asyncio
async def test_reused_pid_group_is_not_killed_or_declared_stopped(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX process-group observation")
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server, entry = api_client(), execution_entry()
    value, lease = observations.prepare(server, entry)
    observations.spawned(server, value, os.getpid())
    value["process_group"] = os.getpgrp()  # A different living lifetime at the recorded group ID.
    observations._write(observations._root(server) / (value["observation_id"] + ".json"), value)
    lease.close()
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("Recovery must never signal an unowned PID"))
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("Recovery must never signal an unowned group"))
    await observations.recover(server)
    assert list(observations._root(server).glob("*.json"))
    assert all(not call.kwargs["outcomes"] for call in server.tasks.reconcile_attempts.await_args_list)


@pytest.mark.asyncio
async def test_stopped_cancellation_uses_exact_ack(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    entry = execution_entry()
    entry.status, entry.error = "terminating", "cancelled"
    server = api_client(entry)
    value, lease = observations.prepare(server, entry)
    lease.close()
    monkeypatch.setattr(observations, "_science_stopped", lambda _: True)
    await observations.recover(server)
    server.tasks.termination_confirmed.assert_awaited_once_with(entry, "cancelled")
    assert not list(observations._root(server).glob("*.json"))
    assert all(not call.kwargs["outcomes"] for call in server.tasks.reconcile_attempts.await_args_list)


@pytest.mark.asyncio
async def test_cancellation_racing_failure_keeps_journal_until_physical_ack(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    entry = execution_entry()
    server = api_client(entry)
    value, lease = observations.prepare(server, entry)
    lease.close()
    monkeypatch.setattr(observations, "_science_stopped", lambda _: True)
    original = server.tasks.reconcile_attempts.side_effect
    async def cancel_during_failure(*, claims, outcomes, **kwargs):
        if outcomes:
            entry.status, entry.error = "terminating", "cancelled during reconciliation"
            return {"outcomes": [{"execution": value["execution"], "state": "terminal"}]}
        return await original(claims=claims, outcomes=outcomes, **kwargs)
    server.tasks.reconcile_attempts.side_effect = cancel_during_failure
    retained = await observations.recover(server)
    assert observations.execution_key(value["execution"]) in retained
    assert list(observations._root(server).glob("*.json"))
    server.tasks.termination_confirmed.assert_not_awaited()
    await observations.recover(server)
    server.tasks.termination_confirmed.assert_awaited_once_with(entry, "cancelled during reconciliation")
    assert not list(observations._root(server).glob("*.json"))


def test_nested_child_ignores_retired_parent_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    entry, server = execution_entry(), api_client()
    value, lease = observations.prepare(server, entry)
    lease.close()
    observations.retire(server, value)
    monkeypatch.setenv("SIMSTACK_NATIVE_OBSERVATION_ID", value["observation_id"])
    monkeypatch.setenv("SIMSTACK_NATIVE_OBSERVATION_EXECUTION", json.dumps(value["execution"]))
    child = execution_entry()
    child.id = "987654321098765432109876"
    observations.adopt(server, child)
    with pytest.raises(FileNotFoundError):
        observations.adopt(server, entry)


@pytest.mark.asyncio
async def test_foreign_actor_and_mismatched_attempt_cannot_retire_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server, entry = api_client(), execution_entry()
    value, lease = observations.prepare(server, entry)
    lease.close()
    server.requests.bootstrap_data = {"account_id": "different-account"}
    await observations.recover(server)
    server.tasks.reconcile_attempts.assert_not_awaited()
    server.requests.bootstrap_data = {"account_id": "account"}
    server.tasks.reconcile_attempts.side_effect = None
    server.tasks.reconcile_attempts.return_value = {"claims": [{
        "execution": {**value["execution"], "claim_id": "other-claim"}, "state": "stale"}]}
    await observations.recover(server)
    assert list(observations._root(server).glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 409, 503])
async def test_rejected_missing_worker_outcome_retains_original_request(tmp_path, monkeypatch, status):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server, entry = api_client(), execution_entry()
    value, lease = observations.prepare(server, entry)
    lease.close()
    monkeypatch.setattr(observations, "_science_stopped", lambda _: True)
    normal = server.tasks.reconcile_attempts.side_effect
    async def unavailable(*, claims, outcomes, **kwargs):
        if outcomes:
            raise RunnerAPIError("outcome unavailable", status_code=status)
        return await normal(claims=claims, outcomes=outcomes, **kwargs)
    server.tasks.reconcile_attempts.side_effect = unavailable
    await observations.recover(server)
    assert list(observations._root(server).glob("*.json"))
    server.tasks.reconcile_attempts.side_effect = normal
    await observations.recover(server)
    outcome_calls = [call for call in server.tasks.reconcile_attempts.await_args_list if call.kwargs["outcomes"]]
    assert len(outcome_calls) == 2
    assert outcome_calls[0].kwargs == outcome_calls[1].kwargs
    assert not list(observations._root(server).glob("*.json"))


@pytest.mark.asyncio
async def test_windows_native_dispatch_keeps_flags_job_and_inherited_handles(monkeypatch):
    from simstack.core.services import node_execution_service as execution_service
    from simstack.runner_runtime import windows_science_job
    from unittest.mock import Mock
    monkeypatch.setattr(execution_service, "os", SimpleNamespace(name="nt", getcwd=lambda: "project"))
    process = SimpleNamespace(pid=123)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(windows_science_job, "spawn_job_process", spawn)
    startup = object()
    environment = {"SIMSTACK_WINDOWS_SCIENCE_JOB": "exact-job"}
    assert await execution_service._spawn_native_process("python", "worker.py", env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup,
        creationflags=0x08000200, start_new_session=False) is process
    assert spawn.call_args.kwargs == {"name": "exact-job", "cwd": "project", "env": environment,
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "startupinfo": startup,
        "retain_in_child": True, "creationflags": 0x08000200}


@pytest.mark.parametrize("active", [None, 0, 1])
def test_windows_missing_job_is_not_a_stopped_process_barrier(monkeypatch, active):
    from simstack.runner_runtime import windows_science_job
    from simstack.runner_runtime.pending_execution_failures import cleanup_scope
    from unittest.mock import Mock
    monkeypatch.setattr(observations, "os", SimpleNamespace(name="nt"))
    job = SimpleNamespace(active_processes=lambda: active, close=Mock())
    constructor = Mock(side_effect=FileNotFoundError() if active is None else None, return_value=job)
    monkeypatch.setattr(windows_science_job, "WindowsJob", constructor)
    assert observations._science_stopped({"windows_job": "exact-job", "scope": cleanup_scope()}) is (active == 0)
    if active is not None:
        job.close.assert_called_once()


@pytest.mark.parametrize("scope", [None, {"hostname": "other-host"},
    {"hostname": "original-host", "pid_namespace": "other-namespace"}])
@pytest.mark.parametrize("windows", [False, True])
def test_unknown_or_foreign_scope_never_uses_local_process_absence(monkeypatch, scope, windows):
    from simstack.runner_runtime import pending_execution_failures, windows_science_job
    monkeypatch.setattr(pending_execution_failures, "cleanup_scope",
        lambda: {"hostname": "original-host", "pid_namespace": "original-namespace"})
    monkeypatch.setattr(observations, "os", SimpleNamespace(name="nt" if windows else "posix"))
    def forbidden(*args, **kwargs):
        pytest.fail("A foreign/unknown process scope cannot provide local stop proof")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(windows_science_job, "WindowsJob", forbidden)
    assert not observations._science_stopped({"scope": scope,
        "process_group": 54321, "windows_job": "original-job"})


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "terminal", "stale"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_foreign_scope_retains_active_observation_but_allows_exact_history_retirement(
    monkeypatch, tmp_path, state, unknown,
):
    from simstack.runner_runtime import pending_execution_failures
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server, entry = api_client(), execution_entry()
    value, lease = observations.prepare(server, entry)
    assert value["scope"] == pending_execution_failures.cleanup_scope()
    lease.close()
    path = observations._root(server) / (value["observation_id"] + ".json")
    if unknown:
        value.pop("scope")
        observations._write(path, value)
    else:
        monkeypatch.setattr(pending_execution_failures, "cleanup_scope", lambda: {"hostname": "other-host"})
    server.tasks.reconcile_attempts.side_effect = None
    server.tasks.reconcile_attempts.return_value = {"claims": [
        {"execution": value["execution"], "state": state, "task": entry}]}
    def forbidden(*args, **kwargs):
        pytest.fail("Foreign/unknown recovery must not inspect local processes")
    monkeypatch.setattr(subprocess, "run", forbidden)
    retained = await observations.recover(server)
    assert path.exists() is (state == "active")
    assert (observations.execution_key(value["execution"]) in retained) is (state == "active")
    assert path.with_suffix(".lease").exists() is (state == "active")
    server.tasks.reconcile_attempts.assert_awaited_once_with(claims=[value["execution"]], outcomes=[])
    server.tasks.termination_confirmed.assert_not_awaited()


@pytest.mark.parametrize("unknown", [False, True])
def test_adoption_cannot_rebind_original_observation_to_another_process_scope(
    monkeypatch, tmp_path, unknown,
):
    from simstack.runner_runtime import pending_execution_failures
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server, entry = api_client(), execution_entry()
    value, lease = observations.prepare(server, entry)
    lease.close()
    path = observations._root(server) / (value["observation_id"] + ".json")
    if unknown:
        value.pop("scope")
        observations._write(path, value)
    else:
        monkeypatch.setattr(pending_execution_failures, "cleanup_scope", lambda: {"hostname": "other-host"})
    before = path.read_bytes()
    monkeypatch.setenv("SIMSTACK_NATIVE_OBSERVATION_ID", value["observation_id"])
    monkeypatch.setenv("SIMSTACK_NATIVE_OBSERVATION_EXECUTION", json.dumps(value["execution"]))
    with pytest.raises(ValueError, match="differs from its admitted execution"):
        observations.adopt(server, entry)
    with pytest.raises(ValueError, match="unknown or different process scope"):
        observations.spawned(server, value, os.getpid())
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "completed", "failed", "terminating"])
async def test_foreign_local_scope_cannot_acknowledge_its_original_owner(monkeypatch, tmp_path, status):
    from simstack.runner_runtime import pending_execution_failures
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    entry = execution_entry()
    entry.status = status
    server = api_client(entry)
    server.requests.authorization, server.requests.token = "local", "local-token"
    value, lease = observations.prepare(server, entry)
    lease.close()
    value["process_id"] = 54321
    entry._execution_owner = {"execution": value["execution"], "process_id": value["process_id"]}
    path = observations._root(server) / (value["observation_id"] + ".json")
    observations._write(path, value)
    server.tasks.get_task = AsyncMock(return_value=entry)
    server.tasks.recover_local_owner = AsyncMock()
    monkeypatch.setattr(pending_execution_failures, "cleanup_scope", lambda: {"hostname": "other-host"})
    retained = await observations.recover(server)
    assert path.exists() and observations.execution_key(value["execution"]) in retained
    server.tasks.recover_local_owner.assert_not_awaited()
    server.tasks.termination_confirmed.assert_not_awaited()
    assert entry.status == status


@pytest.mark.asyncio
async def test_command_records_its_session_before_exec_without_parent_pid_write(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX host command observation")
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    entry = execution_entry()
    value, lease = observations.prepare(server, entry)
    command = observations.observed_command(server, value, ["/bin/sleep", "60"])
    process = None
    try:
        with lease, observations.inherit(lease, {}) as ownership:
            process = subprocess.Popen(command, start_new_session=True, **ownership)
        path = observations._root(server) / (value["observation_id"] + ".json")
        for _ in range(200):
            recorded = json.loads(path.read_text())
            if recorded.get("process_id") is not None:
                break
            await asyncio.sleep(.01)
        assert recorded["process_id"] == recorded["process_group"] == process.pid
        assert _reserve_path(path.with_suffix(".lease")) is None
        await observations.recover(server)
        server.tasks.reconcile_attempts.assert_not_awaited()
        process.kill()
        await asyncio.to_thread(process.wait)
        await observations.recover(server)
        assert not path.exists()
        assert server.tasks.reconcile_attempts.await_args.kwargs["outcomes"][0]["execution"] == server.tasks.execution(entry)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait)
        lease.close()
