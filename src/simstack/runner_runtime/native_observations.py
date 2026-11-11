"""Durable native-worker ownership that survives the launching Runner.

A unique, inherited OS lease identifies the process lifetime; a recycled PID
cannot acquire it. The intent is durable before spawn, and the worker keeps the
lease after its separate launch reservation is released at start acknowledgement.
Recovery never signals a PID or invents a lost waitpid exit status.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from simstack.runner_runtime.launch_claims import _actor, _directory, _reserve_path, _write
from simstack.server_api.workflow_tasks import WorkflowTasksAPI

logger = logging.getLogger("NodeRunner")


def _root(server):
    root = _directory(server.requests.server_url) / "native-observations"
    root.mkdir(exist_ok=True, mode=0o700)
    return root


def execution_key(execution):
    return tuple(execution.get(key) for key in ("task_id", "generation", "claim_id", "start_id"))


def _same_scope(value):
    from simstack.runner_runtime.pending_execution_failures import cleanup_scope
    scope = value.get("scope")
    return scope is not None and scope == cleanup_scope()


def prepare(server, entry):
    """Persist ownership before a child can start or acknowledge its launch."""
    execution = WorkflowTasksAPI.execution(entry)
    if not all(execution.get(key) for key in ("task_id", "claim_id", "start_id")):
        raise ValueError("Native observation requires a complete execution identity")
    identity = uuid4().hex
    path = _root(server) / (identity + ".json")
    lease = _reserve_path(path.with_suffix(".lease"))
    if lease is None:
        raise RuntimeError("Native observation lease is already owned")
    from simstack.runner_runtime.pending_execution_failures import cleanup_scope
    value = {"journal_version": 1, "actor": _actor(server), "scope": cleanup_scope(),
             "execution": execution, "observation_id": identity,
             "request_id": str(uuid4()), "process_id": None}
    try:
        if os.name == "nt":
            from simstack.runner_runtime.windows_science_job import job_name
            value["windows_job"] = job_name(server, entry)
        _write(path, value)
    except BaseException:
        lease.close()
        raise
    return value, lease


@contextmanager
def inherit(lease, ownership):
    """Add the independent observation lease to the atomic spawn inheritance."""
    if os.name != "nt":
        yield {**ownership, "pass_fds": (*ownership.get("pass_fds", ()), lease.fileno())}
        return
    import msvcrt
    import subprocess

    handle = msvcrt.get_osfhandle(lease.fileno())
    startup = ownership.get("startupinfo") or subprocess.STARTUPINFO()
    handles = list((startup.lpAttributeList or {}).get("handle_list", []))
    startup.lpAttributeList = {**(startup.lpAttributeList or {}), "handle_list": [*handles, handle]}
    os.set_handle_inheritable(handle, True)
    try:
        yield {**ownership, "startupinfo": startup}
    finally:
        os.set_handle_inheritable(handle, False)


@contextmanager
def observe_local_launch(server, entry, environment, ownership, *, enabled):
    """Keep local native ownership after its independent start lease closes."""
    if not enabled:
        yield ownership
        return
    value, lease = prepare(server, entry)
    object.__setattr__(entry, "_native_observation", value)
    environment["SIMSTACK_NATIVE_OBSERVATION_ID"] = value["observation_id"]
    environment["SIMSTACK_NATIVE_OBSERVATION_EXECUTION"] = json.dumps(value["execution"])
    with lease, inherit(lease, ownership) as inherited:
        yield inherited


def spawned(server, value, pid):
    if not _same_scope(value):
        raise ValueError("Native observation belongs to an unknown or different process scope")
    value["process_id"] = pid  # Diagnostic only, never authority to signal a process.
    value["process_group"] = pid if os.name != "nt" else None
    _write(_root(server) / (value["observation_id"] + ".json"), value)


def observed_command(server, value, command):
    """Record a host session before exec, even if its Runner dies during spawn."""
    return [sys.executable, str(Path(__file__).resolve()),
            str(_root(server) / (value["observation_id"] + ".json")), *command]


def _exec_observed_command(path, command):
    path = Path(path)
    value = json.loads(path.read_text())
    if os.name == "nt" or os.getpgrp() != os.getpid():
        raise RuntimeError("Observed command must own its POSIX process session")
    if (not command or value.get("journal_version") != 1 or not _same_scope(value)
            or value.get("observation_id") != path.stem):
        raise ValueError("Invalid observed command intent")
    value["process_id"] = value["process_group"] = os.getpid()
    _write(path, value)
    os.execvpe(command[0], command, os.environ)


def adopt(server, entry):
    """The worker records its session before Server can acknowledge its start."""
    identity = os.environ.get("SIMSTACK_NATIVE_OBSERVATION_ID", "")
    if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        return
    if json.loads(os.environ.get("SIMSTACK_NATIVE_OBSERVATION_EXECUTION", "null")) != WorkflowTasksAPI.execution(entry):
        return  # The marker belongs to a parent; its journal may already be retired.
    path = _root(server) / (identity + ".json")
    value = json.loads(path.read_text())
    if (value.get("journal_version") != 1 or value.get("observation_id") != identity
            or value.get("actor") != _actor(server)
            or value.get("execution") != WorkflowTasksAPI.execution(entry)
            or not _same_scope(value)):
        raise ValueError("Native worker observation differs from its admitted execution")
    if os.name != "nt" and os.getpgrp() != os.getpid():
        raise RuntimeError("Native worker does not own its recorded process session")
    spawned(server, value, os.getpid())


def _science_stopped(value):
    """Observe only; a detached/reused process group is never ours to signal."""
    if not _same_scope(value):
        return False  # Local absence says nothing about another host/namespace.
    group = value.get("process_group")
    if os.name == "nt":
        if not value.get("windows_job"):
            return False
        from simstack.runner_runtime.windows_science_job import WindowsJob
        try:
            job = WindowsJob(value["windows_job"])
        except FileNotFoundError:
            return False  # Last-close cleanup may still be asynchronous.
        try:
            return job.active_processes() == 0
        finally:
            job.close()
    if type(group) is not int or group <= 0:
        return False
    result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="],
        capture_output=True, text=True, check=False, timeout=5, start_new_session=True)
    if result.returncode != 0:
        return False
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not rows or any(len(row) != 3 or not row[0].isdigit() or not row[1].isdigit() for row in rows):
        return False
    return not any(int(row[1]) == group and not row[2].startswith("Z") for row in rows)


def retire(server, value):
    path = _root(server) / (value["observation_id"] + ".json")
    path.unlink(missing_ok=True)
    try:
        path.with_suffix(".lease").unlink(missing_ok=True)
    except PermissionError:
        # Windows retains the exclusive inherited handle until process exit.
        pass


async def _recover_local(server, value):
    """Publish stopped ownership through the same narrow local recovery gate."""
    import asyncio
    from simstack.server_api.http_transport import RunnerAPIError

    execution = value["execution"]
    try:
        current = await server.tasks.get_task(execution["task_id"])
    except RunnerAPIError as exc:
        if exc.status_code == 404:
            return True  # DELETE already required the exact physical-stop ACK.
        raise
    observed = server.tasks.execution(current)
    if any(observed.get(key) != execution.get(key) for key in ("task_id", "generation", "claim_id")):
        return True  # A replaced execution never receives this old observation.
    if getattr(current, "_execution_start_id", None) is None:
        # Active prestart needs claim fencing; cancellation already rejects a
        # late START, but still needs this owner's physical-stop observation.
        if current.status not in {"completed", "failed", "timeout", "terminating"}:
            return False
        if not getattr(current, "_execution_owner", None):
            return current.status != "terminating"
    if observed != execution:
        return True
    owner = getattr(current, "_execution_owner", None)
    if owner != {"execution": execution, "process_id": value.get("process_id")}:
        return False
    if not await asyncio.to_thread(_science_stopped, value):
        return False
    result = await server.tasks.recover_local_owner(
        {**execution, "process_id": value["process_id"]}, native_stopped=True)
    return result.get("state") == "terminal"


async def recover(server, *, live=(), task_id=None):
    """Reconcile stopped original workers, retaining evidence across API outages."""
    from simstack.server_api.http_transport import RunnerAPIError

    actor = _actor(server)
    retained = set()
    for path in sorted(_root(server).glob("*.json")):
        try:
            value = json.loads(path.read_text())
            if (not isinstance(value, dict) or value.get("journal_version") != 1
                    or value.get("observation_id") != path.stem
                    or len(path.stem) != 32 or any(c not in "0123456789abcdef" for c in path.stem)
                    or not isinstance(value.get("actor"), dict)
                    or not isinstance(value.get("execution"), dict)
                    or not all(value["execution"].get(key) for key in ("task_id", "claim_id", "start_id"))):
                logger.warning("Unknown native observation %s retained", path.name)
                continue
            local_actor = value.get("actor", {}).get("authorization") == "local"
            own_local = (local_actor and actor["authorization"] in {"user", "local"}
                and all(value["actor"].get(key) == actor[key]
                        for key in ("server_url", "account_id", "resource")))
            if value.get("actor") != actor and not own_local:
                continue
            if task_id is not None and value["execution"]["task_id"] != str(task_id):
                continue
            key = execution_key(value["execution"])
            retained.add(key)
            if path.stem in live:
                continue
            if not path.with_suffix(".lease").exists():
                logger.warning("Native observation %s has no verifiable lease; retained", path.name)
                continue
            lease = _reserve_path(path.with_suffix(".lease"))
            if lease is None:
                continue  # The original spawn still owns this exact lifetime.
            try:
                if local_actor:
                    if await _recover_local(server, value):
                        retire(server, value)
                        retained.discard(key)
                    continue
                execution = value["execution"]
                result = await server.tasks.reconcile_attempts(claims=[execution], outcomes=[])
                state = next((item.get("state") for item in result.get("claims", [])
                              if item.get("execution") == execution), None)
                entry = next((item.get("task") for item in result.get("claims", [])
                              if item.get("execution") == execution), None)
                native_owner = getattr(entry, "_execution_native_controller", False)
                if state == "stale" or state == "terminal" and not native_owner:
                    retire(server, value)
                    retained.discard(key)
                    continue
                if state in {"claimed", "recoverable"}:
                    # No start was acknowledged. The launch journal still owns
                    # safe retry of this claim; a pre-spawn crash is not failure.
                    retire(server, value)
                    retained.discard(key)
                    continue
                if state not in {"active", "terminal"}:
                    continue
                import asyncio
                if not await asyncio.to_thread(_science_stopped, value):
                    logger.warning("Native observation %s cannot verify stopped scientific descendants; retained", path.name)
                    continue
                if (entry is not None and (entry.status == "terminating" or state == "terminal" and native_owner)
                        and server.tasks.execution(entry) == execution):
                    await server.tasks.termination_confirmed(entry, entry.error or "Terminated by user")
                    retire(server, value)
                    retained.discard(key)
                    continue
                outcome = {"execution": execution, "request_id": value["request_id"],
                           "phase": "subprocess", "return_kind": "exception",
                           "error": "Original native worker disappeared without recording its outcome; "
                                    "exit code and signal are unavailable after Runner restart",
                           "exit_code": None, "signal": None}
                result = await server.tasks.reconcile_attempts(claims=[], outcomes=[outcome],
                                                               request_id=value["request_id"])
                outcome_state = next((item.get("state") for item in result.get("outcomes", [])
                                      if item.get("execution") == execution), None)
                if outcome_state == "terminal":
                    # Cancellation can race the claims read. Reconciliation
                    # retains late failure evidence for TERMINATING too; that
                    # acknowledgement is not proof of a terminal task.
                    current = await server.tasks.reconcile_attempts(claims=[execution], outcomes=[])
                    outcome_state = next((item.get("state") for item in current.get("claims", [])
                                          if item.get("execution") == execution), None)
                if outcome_state in {"recorded", "terminal", "stale"}:
                    if native_owner and outcome_state != "stale":
                        await server.tasks.termination_confirmed(entry, entry.error or "Scientific execution stopped")
                    retire(server, value)
                    retained.discard(key)
            finally:
                lease.close()
        except (RunnerAPIError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            logger.warning("Native observation %s awaits reconciliation", path.name, exc_info=True)
    return retained


if __name__ == "__main__":
    _exec_observed_command(sys.argv[1], sys.argv[2:])
