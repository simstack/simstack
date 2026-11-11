"""Small durable execution failures, including failures before API bootstrap."""

from __future__ import annotations

import json
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger("NodeRunner")


def cleanup_scope():
    """A shared journal must not mistake another host's empty PID group for proof."""
    import socket
    import sys
    scope = {"hostname": socket.gethostname()}
    if sys.platform.startswith("linux"):
        try:
            scope["pid_namespace"] = os.readlink("/proc/self/ns/pid")
        except OSError:
            return None
    return scope


def _actor(server=None, resource=None):
    from urllib.parse import urlsplit

    if server is not None:
        requests = server.requests
        url, role = requests.server_url, requests.authorization
        resource = resource or requests.resource
        token = requests.token if role == "local" else None
        bootstrap = requests.bootstrap_data
        account_id = bootstrap.get("account_id") if bootstrap else None
    else:
        url = os.environ.get("SIMSTACK_SERVER_URL", "")
        token = os.environ.get("SIMSTACK_LOCAL_EXECUTION_TOKEN")
        role = "local" if token else "runner"
        resource = resource or os.environ.get("SIMSTACK_EXECUTION_RESOURCE")
        account_id = os.environ.get("SIMSTACK_EXECUTION_ACCOUNT_ID")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not resource
        or not account_id
    ):
        return None
    actor = {
        "server_url": url.rstrip("/"),
        "account_id": str(account_id),
        "authorization": role,
        "resource": str(resource),
    }
    if role == "local":
        if not token:
            return None
        actor["credential_fingerprint"] = hashlib.sha256(token.encode()).hexdigest()
    return actor


def failure_directory():
    explicit = os.environ.get("SIMSTACK_PENDING_FAILURE_DIR")
    if explicit:
        return Path(explicit)
    workdir = os.environ.get("SIMSTACK_RUNNER_WORKDIR")
    return Path(workdir) / ".pending-execution-failures" if workdir else None


def record_failure(
    error,
    *,
    phase,
    execution=None,
    exit_code=None,
    signal=None,
    server=None,
    resource=None,
    return_kind="exception",
    rejected_status=None,
    request_id=None,
    observed_at=None,
    cleanup=None,
):
    from simstack.runner_runtime.launch_claims import _write
    from simstack.util.sanitized_output import sanitized_tail

    execution = execution or {
        "task_id": os.environ.get("SIMSTACK_EXECUTION_TASK"),
        "generation": os.environ.get("SIMSTACK_EXECUTION_GENERATION"),
        "claim_id": os.environ.get("SIMSTACK_EXECUTION_CLAIM"),
        "start_id": os.environ.get("SIMSTACK_EXECUTION_START"),
    }
    directory = failure_directory()
    if directory is None or not all(
        execution.get(key) for key in ("task_id", "claim_id", "start_id")
    ):
        return None
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {
        "journal_version": 1,
        "actor": _actor(server, resource),
        "request_id": request_id or str(uuid4()),
        "execution": execution,
        "phase": phase,
        "return_kind": return_kind,
        "error": sanitized_tail(str(error), None),
        "exit_code": exit_code,
        "signal": signal,
        "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        "rejected_status": rejected_status,
    }
    if cleanup is not None:
        value["cleanup"] = cleanup
    path = directory / (value["request_id"] + ".json")
    _write(path, value)
    return path


def record_observed_failure(error, *, phase, execution, server, resource=None,
                            exit_code=None, signal=None, return_kind="exception", cleanup=None):
    """Retain a known physical exit before any uncertain outcome/API read."""
    from simstack.util.sanitized_output import sanitized_tail

    outcome = {"request_id": str(uuid4()), "execution": execution, "phase": phase,
               "error": sanitized_tail(str(error), None), "return_kind": return_kind,
               "exit_code": exit_code, "signal": signal,
               "observed_at": datetime.now(timezone.utc).isoformat()}
    if cleanup is not None:
        outcome["cleanup"] = cleanup
    try:
        path = record_failure(server=server, resource=resource, **outcome)
    except OSError:
        logger.warning("Could not journal observed task %s exit", execution["task_id"], exc_info=True)
        path = None
    return path, outcome


async def _replay_safe(server, outcome):
    """A controller exit alone cannot fail still-running scientific descendants."""
    cleanup = outcome.get("cleanup")
    if cleanup is None:
        return True
    from simstack.server_api.http_transport import RunnerAPIError
    import asyncio
    import subprocess
    try:
        if (isinstance(cleanup, dict) and not cleanup.get("container")
                and cleanup.get("scope") is not None and cleanup["scope"] == cleanup_scope()):
            from simstack.runner_runtime.native_observations import _science_stopped
            try:
                if await asyncio.to_thread(_science_stopped, cleanup):
                    return True
            except (OSError, ValueError, subprocess.SubprocessError):
                # Missing local process visibility cannot manufacture proof,
                # but a terminal exact attempt can still accept evidence.
                logger.warning("Task %s cannot verify its original native group",
                               outcome["execution"]["task_id"], exc_info=True)
        execution = outcome["execution"]
        try:
            result = await server.tasks.reconcile_attempts(claims=[execution], outcomes=[])
        except RunnerAPIError as exc:
            if exc.status_code != 403 or server.requests.authorization != "local":
                raise
            # Retired local credentials cannot reconcile active claims, but
            # retain narrowly authorized access to their exact attempt history.
            result = await server.requests.read(f"/tasks/{execution['task_id']}/outcome",
                **{key: value for key, value in execution.items() if key != "task_id"})
            recorded = result.get("outcome")
            return (isinstance(recorded, dict) and recorded.get("execution") == execution
                    and recorded.get("status") in {"completed", "failed", "timeout", "terminating"})
        for item in result.get("claims", []):
            if item.get("execution") != execution:
                continue
            # These states accept only historical evidence, never a new failure
            # of active science. They do not acknowledge physical termination.
            task = item.get("task")
            status = task.get("status") if isinstance(task, dict) else getattr(task, "status", None)
            return item.get("state") in {"terminal", "stale"} or status == "terminating"
    except (RunnerAPIError, OSError, ValueError, subprocess.SubprocessError):
        logger.warning("Task %s awaits verified cleanup before exit reconciliation",
                       outcome["execution"]["task_id"], exc_info=True)
    return False


async def reconcile_observed_failure(server, pending):
    """Return whether Server accepted this receipt; keep its exact identity on retry."""
    from simstack.server_api.http_transport import RunnerAPIError

    path, outcome = pending
    if not await _replay_safe(server, outcome):
        return False
    payload = {key: value for key, value in outcome.items() if key != "cleanup"}
    try:
        await server.tasks.reconcile_attempts(claims=[], outcomes=[payload],
                                             request_id=outcome["request_id"])
        if path is not None:
            acknowledge_failure(path)
        return True
    except (RunnerAPIError, OSError, ValueError) as exc:
        if path is not None and getattr(exc, "status_code", None) in {403, 409}:
            from simstack.runner_runtime.launch_claims import _write
            try:
                receipt = json.loads(path.read_text())
                _write(path, {**receipt, "rejected_status": exc.status_code})
            except (OSError, ValueError):
                logger.warning("Could not retain observed outcome rejection", exc_info=True)
        logger.warning("Observed task %s exit awaits reconciliation", outcome["execution"]["task_id"], exc_info=True)
        return False


def pending_failures():
    directory = failure_directory()
    if directory is not None and directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                logger.warning(
                    "Unreadable execution failure journal %s retained", path.name
                )
                continue
            yield path, value


def acknowledge_failure(path):
    path.unlink(missing_ok=True)


async def reconcile_failures(server, *, resource=None):
    actor = _actor(server, resource)
    for path, outcome in pending_failures():
        if (
            not isinstance(outcome, dict)
            or outcome.get("journal_version") != 1
            or outcome.get("actor") is None
        ):
            logger.warning(
                "Unscoped or unknown execution failure journal %s retained", path.name
            )
            continue
        if actor is None or outcome["actor"] != actor:
            continue
        if not await _replay_safe(server, outcome):
            continue
        payload = {
            key: value
            for key, value in outcome.items()
            if key not in {"journal_version", "actor", "rejected_status", "cleanup"}
        }
        from simstack.server_api.http_transport import RunnerAPIError
        try:
            await server.tasks.reconcile_attempts(
                claims=[],
                outcomes=[payload],
                request_id=outcome["request_id"],
            )
        except RunnerAPIError as exc:
            if exc.status_code not in {403, 409} or outcome.get("rejected_status") not in {403, 409}:
                raise
            # Preserve rejected evidence for its original attempt without
            # blocking reconciliation and launches for unrelated work.
            logger.warning("Execution failure journal %s remains rejected (%s)", path.name, exc.status_code)
            continue
        acknowledge_failure(path)
