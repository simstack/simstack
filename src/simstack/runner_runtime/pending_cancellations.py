"""Durable dependency cancellation commands for waiters without local handles."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from uuid import uuid4

from simstack.runner_runtime.launch_claims import _actor, _directory, _reserve_path, _write
from simstack.server_api.http_transport import RunnerAPIError

logger = logging.getLogger("NodeRunner")
_retries = {}
_owned_processes = {}


def record_cancellation(server, entry, parent_execution, reason, *, workdir=None):
    if not parent_execution:
        raise ValueError("Original parent execution is required for cancellation")
    directory = _directory(server.requests.server_url) / "cancellations"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {"journal_version": 1, "actor": _actor(server), "request_id": str(uuid4()),
        "parent_execution": dict(parent_execution), "child_id": str(entry.id),
        "generation": getattr(entry, "_execution_generation", None), "reason": reason,
        "workdir": str(workdir) if workdir is not None else None}
    path = directory / (value["request_id"] + ".json")
    _write(path, value)
    return path


async def _finish_owned_cancellation(server, path, value, entry):
    """Use the returned exact attempt to resolve an existing physical handle."""
    from simstack.core.definitions import TaskStatus
    from simstack.core.services.task_termination import owned_local_process, stop_task_execution
    terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIME_OUT}
    if entry is None or entry.status != TaskStatus.TERMINATING:
        return True  # Shared dependency, stale generation, or already terminal.
    execution = server.tasks.execution(entry)
    process = _owned_processes.get(path) or owned_local_process(entry)
    owner_server = getattr(process, "_simstack_execution_server", None) or server
    owned = value.get("owned_execution")
    if owned is not None and owned != execution:
        raise ValueError("Cancellation cleanup differs from its original owned attempt")
    if process is None and owned is None:
        return True  # Server delegated cancellation to another physical owner.
    if process is not None:
        # Keep the actual handle through physical-stop and acknowledgement
        # outages, even if its launcher's wait operation exits meanwhile.
        _owned_processes[path] = process
    if not value.get("stopped_execution"):
        if process is None:
            outcome = await server.tasks.get_outcome(entry)
            return outcome is not None and outcome.status in terminal
        if owned is None:
            value = {**value, "owned_execution": execution}
            _write(path, value)
        await stop_task_execution(entry, server=owner_server, runner_pid=os.getpid(),
            workdir=value.get("workdir") or os.environ.get("SIMSTACK_RUNNER_WORKDIR"), local_process=process)
        value = {**value, "stopped_execution": execution}
        _write(path, value)
    try:
        await owner_server.tasks.termination_confirmed(entry, value["reason"])
    except RunnerAPIError as exc:
        if exc.status_code not in {403, 409}:
            raise
        # Its owning monitor can ACK concurrently. Only Server's original
        # terminal outcome makes that conflict a completed cancellation.
        outcome = await owner_server.tasks.get_outcome(entry)
        if outcome is None or outcome.status not in terminal:
            raise
    return True


async def replay_cancellation(server, path):
    # Concurrent background/Runner replay uses the same idempotency identity;
    # this lease avoids redundant HTTP while retaining the command on failure.
    reservation = _reserve_path(path.with_suffix(".replay"))
    if reservation is None:
        return False
    try:
        if not path.exists():
            return True
        value = json.loads(path.read_text())
        if value.get("journal_version") != 1 or value.get("actor") != _actor(server):
            return False
        parent = value["parent_execution"]
        entry = await server.tasks.cancel_dependency(parent["task_id"], parent, value["child_id"],
            value["generation"], value["reason"], request_id=value["request_id"])
        if not await _finish_owned_cancellation(server, path, value, entry):
            return False
        path.unlink(missing_ok=True)
        _owned_processes.pop(path, None)
        return True
    finally:
        reservation.close()


def retry_cancellation(server, path):
    if path in _retries:
        return
    async def retry():
        while path.exists():
            await asyncio.sleep(1)
            try:
                if await replay_cancellation(server, path):
                    return
            except RunnerAPIError as exc:
                if exc.status_code is not None and exc.status_code < 500:
                    logger.warning("Cancellation %s remains rejected (%s)", path.name, exc.status_code)
                    return
            except Exception:
                logger.exception("Cancellation %s remains pending", path.name)
    operation = asyncio.create_task(retry())
    _retries[path] = operation
    operation.add_done_callback(lambda _: _retries.pop(path, None))


async def reconcile_cancellations(server):
    directory = _directory(server.requests.server_url) / "cancellations"
    if not directory.exists():
        return
    from simstack.core.services.task_termination import TaskTerminationError
    for path in directory.glob("*.json"):
        try:
            await replay_cancellation(server, path)
        except (RunnerAPIError, TaskTerminationError):
            retry_cancellation(server, path)
        except (OSError, ValueError):
            logger.warning("Unreadable cancellation %s retained", path.name, exc_info=True)


async def drain_cancellations(server, execution):
    """Do not let successful parent exit strand its timed-out dependencies."""
    directory = _directory(server.requests.server_url) / "cancellations"
    if not directory.exists():
        return
    from simstack.core.services.task_termination import TaskTerminationError
    actor = _actor(server)
    while True:
        pending = []
        for path in directory.glob("*.json"):
            try:
                value = json.loads(path.read_text())
            except FileNotFoundError:
                continue  # A concurrent successful replay retired it.
            if value.get("actor") == actor and value.get("parent_execution") == execution:
                pending.append(path)
        if not pending:
            return
        for path in pending:
            try:
                await replay_cancellation(server, path)
            except RunnerAPIError as exc:
                if exc.status_code is not None and exc.status_code < 500:
                    raise
            except TaskTerminationError:
                logger.warning("Cancellation %s awaits verified physical cleanup", path.name)
        await asyncio.sleep(0.1)
