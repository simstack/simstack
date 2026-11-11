import importlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from simstack.models import NodeRegistry, Parameters
from simstack.models.parameters import SlurmParameters
from simstack.util import slurm_observation


@pytest.mark.parametrize("state", ["FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "CANCELLED", "COMPLETED"])
def test_accounting_distinguishes_terminal_outcomes(monkeypatch, state):
    replies = iter(["", f"42|{state}|17:0|2026-11-09T12:00:00|reason\n"])
    monkeypatch.setattr(slurm_observation, "_run", lambda command: next(replies))
    result = slurm_observation.observe_job("42")
    assert result["kind"] == "terminal"
    assert result["state"] == state
    assert result["exit_code"] == 17
    assert result["ended_at"].endswith("+00:00")


def test_absence_and_query_failure_are_unknown(monkeypatch):
    monkeypatch.setattr(slurm_observation, "_run", lambda command: "")
    absent = slurm_observation.observe_job("42")
    assert absent["kind"] == "unknown" and absent["job_absent"]
    def failed(command):
        raise RuntimeError("controller unavailable")
    monkeypatch.setattr(slurm_observation, "_run", failed)
    unknown = slurm_observation.observe_job("42")
    assert unknown["kind"] == "unknown" and not unknown["job_absent"]


@pytest.mark.parametrize("unavailable", ["squeue", "sacct"])
def test_one_failed_scheduler_query_never_establishes_job_absence(monkeypatch, unavailable):
    def query(command):
        if command[0] == unavailable:
            raise RuntimeError("scheduler query unavailable")
        return ""
    monkeypatch.setattr(slurm_observation, "_run", query)
    assert not slurm_observation.observe_job("42", restart_count=1)["job_absent"]


def test_missing_job_uses_successful_full_queue_view(monkeypatch):
    def query(command):
        if command[0] == "squeue":
            assert "--jobs" not in command and "--states=all" in command
            return "99|RUNNING|R|another-task|user|1:23|node01|None\n"
        return ""
    monkeypatch.setattr(slurm_observation, "_run", query)
    assert slurm_observation.observe_job("42", restart_count=1)["job_absent"]


def test_active_queue_job_does_not_require_accounting(monkeypatch):
    calls = []
    def query(command):
        calls.append(command)
        return "42|RUNNING|R|calculation|user|1:23|node01|None\n"
    monkeypatch.setattr(slurm_observation, "_run", query)
    assert slurm_observation.observe_job("42")["kind"] == "active"
    assert len(calls) == 1


@pytest.mark.parametrize("expected,reported", [(0, "0"), (1, "1"), (0, "1"), (1, "2"), (1, "0"), (1, "")])
def test_accounting_is_fenced_by_the_current_scheduler_incarnation(monkeypatch, expected, reported):
    def query(command):
        if command[0] == "squeue":
            raise RuntimeError("controller unavailable")
        assert command[-1].endswith(",Restarts")
        return f"42|FAILED|17:0|2026-11-09T12:00:00|preempted|{reported}\n"
    monkeypatch.setattr(slurm_observation, "_run", query)
    result = slurm_observation.observe_job("42", restart_count=expected)
    assert result["kind"] == ("terminal" if reported and int(reported) >= expected else "unknown")
    if result["kind"] == "terminal":
        assert result["restart_count"] == int(reported)


@pytest.mark.parametrize("latest_state", ["RUNNING", "COMPLETED", "FAILED"])
def test_accounting_uses_latest_restart_before_selecting_an_outcome(monkeypatch, latest_state):
    def query(command):
        if command[0] == "squeue":
            return ""
        return ("42|FAILED|1:0|2026-11-09T12:00:00|old failure|0\n"
            f"42|{latest_state}|0:0|2026-11-09T13:00:00|latest|2\n"
            "42|FAILED|1:0|2026-11-09T12:30:00|older failure|1\n")
    monkeypatch.setattr(slurm_observation, "_run", query)
    result = slurm_observation.observe_job("42", restart_count=0)
    assert result["state"] == latest_state and result["restart_count"] == 2


def test_ambiguous_submission_is_located_by_its_unique_name(monkeypatch):
    identity = str(uuid4())
    replies = iter(["", f"42|ss-{identity}\n"])
    monkeypatch.setattr(slurm_observation, "_run", lambda command: next(replies))
    assert slurm_observation.find_submission(identity, "2026-11-09T12:00:00+00:00") == "42"


def test_submission_uses_accounting_even_when_queue_query_fails(monkeypatch):
    identity = str(uuid4())
    calls = []
    def query(command):
        calls.append(command[0])
        if command[0] == "squeue":
            raise RuntimeError("controller unavailable")
        return f"42|ss-{identity}\n"
    monkeypatch.setattr(slurm_observation, "_run", query)
    assert slurm_observation.find_submission(identity, "2026-11-09T12:00:00+00:00") == "42"
    assert calls == ["squeue", "sacct"]


def test_missing_submission_is_conclusive_only_when_both_sources_work(monkeypatch):
    identity = str(uuid4())
    def query(command):
        if command[0] == "sacct":
            raise RuntimeError("accounting unavailable")
        return ""
    monkeypatch.setattr(slurm_observation, "_run", query)
    with pytest.raises(RuntimeError, match="accounting unavailable"):
        slurm_observation.find_submission(identity, "2026-11-09T12:00:00+00:00")
    monkeypatch.setattr(slurm_observation, "_run", lambda command: "")
    assert slurm_observation.find_submission(identity, "2026-11-09T12:00:00+00:00") is None


def test_unrecognized_scheduler_output_is_not_conclusive_absence(monkeypatch):
    replies = iter(["unexpected response", ""])
    monkeypatch.setattr(slurm_observation, "_run", lambda command: next(replies))
    with pytest.raises(RuntimeError, match="Unexpected scheduler"):
        slurm_observation.find_submission(str(uuid4()), "2026-11-09T12:00:00+00:00")


def test_array_submission_uses_the_queue_base_id_for_grouped_and_started_tasks(monkeypatch):
    identity = str(uuid4())
    def query(command):
        assert command[0] == "squeue"
        # %F reports the shared base for both 42_[1-2] and started 42_3.
        assert command[-1] == "--format=%F|%j"
        return f"42|ss-{identity}\n42|ss-{identity}\n"
    monkeypatch.setattr(slurm_observation, "_run", query)
    assert slurm_observation.find_submission(identity, "2026-11-09T12:00:00+00:00") == "42"


def test_array_submission_accounting_collapses_elements_without_losing_duplicate_detection(monkeypatch):
    identity = str(uuid4())
    array_ids = ["42_1", "42_2"]
    def query(command):
        if command[0] == "squeue":
            return ""
        assert "--array" in command
        assert command[-1] == "--format=JobID,JobName%100"
        return "".join(f"{job}|ss-{identity}\n" for job in array_ids)
    monkeypatch.setattr(slurm_observation, "_run", query)
    assert slurm_observation.find_submission(identity, "2026-11-09T12:00:00+00:00") == "42"
    array_ids.append("43_1")
    with pytest.raises(RuntimeError, match="Multiple Slurm jobs"):
        slurm_observation.find_submission(identity, "2026-11-09T12:00:00+00:00")


def test_pending_array_is_observed_by_queue_base_id(monkeypatch):
    def query(command):
        assert command[0] == "squeue"
        assert command[-1].startswith("--format=%F|")
        return "42|PENDING|PD|array|user|0:00|node01|Resources\n"
    monkeypatch.setattr(slurm_observation, "_run", query)
    result = slurm_observation.observe_job("42")
    assert result["kind"] == "active" and result["state"] == "PENDING"


def test_queue_environment_cannot_hide_pending_array_members(monkeypatch):
    monkeypatch.setenv("SQUEUE_STATES", "RUNNING")
    calls = []

    def query(command, **kwargs):
        calls.append(command)
        if command[0] == "squeue":
            states = next((item.split("=", 1)[1] for item in command if item.startswith("--states=")),
                          kwargs["env"].get("SQUEUE_STATES"))
            output = "42|PENDING|PD|array|user|0:00|node01|Resources\n" if states == "all" else ""
        else:
            output = "42_1|COMPLETED|0:0|2026-11-09T12:00:00|None\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(slurm_observation.subprocess, "run", query)
    result = slurm_observation.observe_job("42")
    assert result["kind"] == "active" and result["state"] == "PENDING"
    assert len(calls) == 1


def test_submission_lookup_environment_cannot_hide_pending_job(monkeypatch):
    monkeypatch.setenv("SQUEUE_STATES", "RUNNING")
    identity = str(uuid4())
    calls = []

    def query(command, **kwargs):
        calls.append(command)
        states = next((item.split("=", 1)[1] for item in command if item.startswith("--states=")),
                      kwargs["env"].get("SQUEUE_STATES"))
        output = f"42|ss-{identity}\n" if command[0] == "squeue" and states == "all" else ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(slurm_observation.subprocess, "run", query)
    assert slurm_observation.find_submission(identity, "2026-11-09T12:00:00+00:00") == "42"
    assert len(calls) == 1


def test_accounting_active_array_member_prevents_terminalization(monkeypatch):
    def query(command):
        if command[0] == "squeue":
            raise RuntimeError("controller unavailable")
        return "42_1|COMPLETED|0:0|2026-11-09T12:00:00|None\n42_2|RUNNING|0:0|Unknown|None\n"
    monkeypatch.setattr(slurm_observation, "_run", query)
    result = slurm_observation.observe_job("42")
    assert result["kind"] == "active" and result["state"] == "RUNNING"


@pytest.mark.parametrize("last_state,last_code", [("COMPLETED", "0:0"), ("FAILED", "17:9")])
def test_terminal_array_aggregates_all_accounting_members(monkeypatch, last_state, last_code):
    replies = iter(["", "42_1|COMPLETED|0:0|2026-11-09T12:00:00|None\n"
                    f"42_2|{last_state}|{last_code}|2026-11-09T12:01:00|member reason\n"])
    monkeypatch.setattr(slurm_observation, "_run", lambda command: next(replies))
    result = slurm_observation.observe_job("42")
    assert result["kind"] == "terminal" and result["state"] == last_state
    assert result["exit_code"] == int(last_code.split(":")[0])
    assert result["signal"] == int(last_code.split(":")[1])
    if last_state == "FAILED":
        assert result["reason"] == "Array tasks: COMPLETED=1, FAILED=1; 42_2: member reason"
    else:
        assert result["reason"] == "Array tasks: COMPLETED=2; 42_1: None"
    assert result["ended_at"] == "2026-11-09T12:01:00+00:00"


def test_partial_array_accounting_cannot_prove_completion_during_queue_outage(monkeypatch):
    def query(command):
        if command[0] == "squeue":
            raise RuntimeError("controller unavailable")
        return "42_1|COMPLETED|0:0|2026-11-09T12:00:00|None\n"
    monkeypatch.setattr(slurm_observation, "_run", query)
    assert slurm_observation.observe_job("42")["kind"] == "unknown"


def test_observing_one_array_element_does_not_include_other_elements(monkeypatch):
    replies = iter(["", "42_1|COMPLETED|0:0|2026-11-09T12:00:00|None\n"
                    "42_2|RUNNING|0:0|Unknown|None\n"])
    monkeypatch.setattr(slurm_observation, "_run", lambda command: next(replies))
    result = slurm_observation.observe_job("42_1")
    assert result["kind"] == "terminal" and result["state"] == "COMPLETED"


def test_unrecognized_array_member_state_does_not_complete_the_array(monkeypatch):
    replies = iter(["", "42_1|COMPLETED|0:0|2026-11-09T12:00:00|None\n"
                    "42_2|UNRECOGNIZED|0:0|Unknown|None\n"])
    monkeypatch.setattr(slurm_observation, "_run", lambda command: next(replies))
    assert slurm_observation.observe_job("42")["kind"] == "unknown"


def test_malformed_queue_and_partial_terminal_array_accounting_stays_unknown(monkeypatch):
    replies = iter(["unexpected response", "42_1|COMPLETED|0:0|2026-11-09T12:00:00|None\n"])
    monkeypatch.setattr(slurm_observation, "_run", lambda command: next(replies))
    assert slurm_observation.observe_job("42")["kind"] == "unknown"


def test_submitter_query_failure_does_not_prove_the_submitter_died(monkeypatch):
    from simstack.runner_runtime import slurm_submission_intents as intents
    from simstack.runner_runtime import pending_execution_failures as failures
    scope = {"hostname": "local"}
    monkeypatch.setattr(failures, "cleanup_scope", lambda: scope)
    value = {"process_id": 42, "process_birth": "original birth", "scope": scope}
    monkeypatch.setattr(intents.os, "kill", lambda *args: None)
    monkeypatch.setattr(intents, "process_identity", lambda pid: None)
    assert intents.submitter_alive(value) is None
    monkeypatch.setattr(intents, "process_identity", lambda pid: "another birth")
    assert intents.submitter_alive(value) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["42;cluster\n", "unrecognized acceptance\n", subprocess.TimeoutExpired("sbatch", 30)])
async def test_submission_does_not_blindly_resubmit(tmp_path, monkeypatch, reply):
    module = importlib.import_module("simstack.core.submit_node")
    runtime = importlib.import_module("simstack.runner_runtime.execution_environment")
    identity = None
    async def reserve(entry, submission_id, **kwargs):
        nonlocal identity
        identity = identity or submission_id
        return {"created": True, "submission_id": identity}
    api = SimpleNamespace(
        scheduler_submission=AsyncMock(side_effect=reserve),
        scheduler_accepted=AsyncMock(), fail_attempt=AsyncMock(),
    )
    config = SimpleNamespace(project_root=tmp_path, workdir=tmp_path, resource="local", python_paths=[], environment_start="", docker=False)
    server = SimpleNamespace(requests=SimpleNamespace(server_url="http://slurm-test", authorization="runner",
        resource="local", bootstrap_data={"account_id": "slurm-test-account"}), slurm=api,
        tasks=SimpleNamespace(fail_attempt=api.fail_attempt, execution=lambda entry: {"task_id": str(entry.id)}))
    monkeypatch.setattr(module, "context", SimpleNamespace(config=config, server=server))
    monkeypatch.setattr(module, "process_is_in_docker", lambda: False)
    monkeypatch.setattr(runtime, "task_resource_config", lambda *args: SimpleNamespace(get_program=lambda *args, **kwargs: {}))
    monkeypatch.setattr(runtime, "task_environment", lambda *args, **kwargs: dict(os.environ))
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr("simstack.runner_runtime.slurm_submission_intents.process_identity", lambda pid: "fixture-process")
    calls = []
    def submit(command, **kwargs):
        calls.append(command)
        process = SimpleNamespace(returncode=0, pid=os.getpid())
        def kill():
            process.returncode = -9
        def communicate(**options):
            if isinstance(reply, Exception) and process.returncode == 0:
                raise reply
            return (reply, "") if isinstance(reply, str) else ("", "")
        process.kill, process.communicate = kill, communicate
        return process
    monkeypatch.setattr(module.subprocess, "Popen", submit)
    entry = NodeRegistry(name="fixture", status="retrieved", function_hash="", arg_hash="input", func_mapping="fixture.run", repository_revision="revision", parameters=Parameters(slurm_parameters=SlurmParameters()))
    assert await module.submit_node(entry)
    script = (tmp_path / entry.name / str(entry.id) / "slurm_script.sh").read_text()
    assert "exec " in script
    assert "wait" not in script
    assert "--signal" not in script
    assert "ss-" + identity in script
    assert await module.submit_node(entry)
    assert len(calls) == 1
    if str(reply).startswith("42;"):
        api.scheduler_accepted.assert_awaited_once_with({"task_id": str(entry.id)}, identity, "42")
    else:
        api.scheduler_accepted.assert_not_awaited()
    api.fail_attempt.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt", reason="Slurm batch execution requires a POSIX host")
def test_normal_batch_exec_preserves_child_exit_code(tmp_path):
    script = tmp_path / "batch.sh"
    script.write_text("#!/usr/bin/env bash\nexec " + sys.executable + " -c 'raise SystemExit(17)'\n")
    assert subprocess.run(["bash", str(script)]).returncode == 17


@pytest.mark.skipif(os.name == "nt", reason="Slurm checkpoint signals require a POSIX host")
def test_checkpoint_does_not_turn_child_failure_into_success(tmp_path):
    import simstack
    script = Path(simstack.__file__).parent / "static" / "slurm_checkpoint.sh"
    scratch, recovery = tmp_path / "scratch", tmp_path / "recovery"
    scratch.mkdir()
    (scratch / "checkpoint.txt").write_text("saved")
    ready = tmp_path / "ready"
    code = "from pathlib import Path; import time; Path(" + repr(str(ready)) + ").touch(); time.sleep(0.5); raise SystemExit(17)"
    proc = subprocess.Popen(["bash", str(script), str(scratch), str(recovery), sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        proc.send_signal(signal.SIGUSR1)
        assert proc.wait(timeout=5) == 17
        assert (recovery / "checkpoint.txt").read_text() == "saved"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(os.name == "nt", reason="Slurm signals require a POSIX host")
@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGINT])
def test_checkpoint_wrapper_forwards_signals_and_reaps_its_process_group(tmp_path, termination):
    import simstack
    script = Path(simstack.__file__).parent / "static" / "slurm_checkpoint.sh"
    scratch, recovery = tmp_path / "scratch", tmp_path / "recovery"
    scratch.mkdir()
    (scratch / "checkpoint.txt").write_text("saved")
    child_pid, grandchild_pid = tmp_path / "child.pid", tmp_path / "grandchild.pid"
    child_signal, grandchild_signal = tmp_path / "child.signal", tmp_path / "grandchild.signal"
    grandchild_code = f'''import os, signal, sys, time
from pathlib import Path
def stop(sig, frame):
    Path({str(grandchild_signal)!r}).write_text(str(sig))
    sys.exit(128 + sig)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
Path({str(grandchild_pid)!r}).write_text(str(os.getpid()))
time.sleep(60)
'''
    child_code = f'''import os, signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", {grandchild_code!r}])
def stop(sig, frame):
    Path({str(child_signal)!r}).write_text(str(sig))
    child.wait(timeout=3)
    sys.exit(128 + sig)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
Path({str(child_pid)!r}).write_text(str(os.getpid()))
time.sleep(60)
'''
    process = subprocess.Popen(["bash", str(script), str(scratch), str(recovery), sys.executable, "-c", child_code])
    owned = []
    try:
        deadline = time.monotonic() + 5
        while not (child_pid.exists() and grandchild_pid.exists()) and time.monotonic() < deadline:
            time.sleep(.01)
        assert child_pid.exists() and grandchild_pid.exists()
        owned = [int(child_pid.read_text()), int(grandchild_pid.read_text())]
        assert os.getpgid(owned[0]) == owned[0] == os.getpgid(owned[1])
        process.send_signal(signal.SIGUSR1)
        deadline = time.monotonic() + 3
        while not (recovery / "checkpoint.txt").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (recovery / "checkpoint.txt").read_text() == "saved"
        process.send_signal(termination)
        assert process.wait(timeout=5) == 128 + termination
        assert child_signal.read_text() == str(termination)
        assert grandchild_signal.read_text() == str(termination)
        for pid in owned:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        for pid in owned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
            process.wait()
