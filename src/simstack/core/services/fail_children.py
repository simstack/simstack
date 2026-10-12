"""Fail and stop child tasks when their parent fails."""

import asyncio
import logging
import os
import platform
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from bson import ObjectId as BsonObjectId
from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.run_docker import docker_cidfile_path, read_container_id
from simstack.models import NodeRegistry

logger = logging.getLogger("NodeRunner")

_ACTIVE_STATUSES = tuple(
    status
    for name in (
        "SUBMITTED",
        "RETRIEVED",
        "RUNNING",
        "SLURM_QUEUED",
        "SLURM_RUNNING",
        "TERMINATING",
    )
    if (status := getattr(TaskStatus, name, None)) is not None
)


def _require_pid(process_id: object, label: str) -> int:
    if type(process_id) is not int or process_id < 1:
        raise ValueError(f"{label} must be a positive int, got {process_id!r}")
    return process_id


def _parent_object_id(parent_id: object) -> ObjectId:
    if isinstance(parent_id, ObjectId):
        return parent_id
    # model_validate_doc keeps pymongo's ObjectId. It is not an instance of
    # odmantic.ObjectId, even though both render as ObjectId('...').
    if isinstance(parent_id, BsonObjectId):
        return ObjectId(parent_id)
    if isinstance(parent_id, str) and ObjectId.is_valid(parent_id):
        return ObjectId(parent_id)
    raise ValueError(
        "parent task id must be an ObjectId, got "
        f"{type(parent_id).__module__}.{type(parent_id).__qualname__} {parent_id!r}"
    )


def _kill_process_tree(process_id: int) -> None:
    """Kill a process and its children. Does not kill the process session."""
    process_id = _require_pid(process_id, "process_id")
    if platform.system() == "Windows":
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    for child_pid in _child_pids(process_id):
        _kill_process_tree(child_pid)
    try:
        os.kill(process_id, getattr(signal, "SIGKILL", 9))
    except ProcessLookupError:
        return
    except OSError as exc:
        logger.warning("Could not kill process %s: %s", process_id, exc)


def _child_pids(process_id: int) -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(process_id)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        logger.warning("Could not list children of %s: %s", process_id, exc)
        return []
    children: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.isdigit():
            children.append(int(stripped))
    return children


async def _run_command(args: list[str]) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await process.communicate()
    except OSError as exc:
        logger.warning("Could not run %s: %s", args[0], exc)
        return
    if process.returncode != 0:
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        logger.warning("%s exited %s: %s", args[0], process.returncode, stderr)


async def _stop_task(entry: NodeRegistry, *, runner_pid: int, extra_pids: tuple[int, ...]) -> None:
    """Stop slurm, docker, and process trees for one task. Does not change status."""
    runner_pid = _require_pid(runner_pid, "runner_pid")
    job_id = entry.job_id
    if job_id is not None:
        if not isinstance(job_id, str):
            raise ValueError(f"Task task_id: {entry.id} job_id must be a string, got {job_id!r}")
        if job_id.isdigit():
            await _run_command(["scancel", job_id])

    workdir = getattr(context.config, "workdir", None)
    if workdir is None:
        raise ValueError("runner workdir is not configured")
    if not entry.name:
        raise ValueError(f"Task task_id: {entry.id} has no name")
    cidfile = docker_cidfile_path(Path(workdir) / entry.name / str(entry.id))
    if cidfile.is_file():
        container_id = read_container_id(cidfile)
        if container_id:
            await _run_command(["docker", "kill", container_id])

    pids: list[int] = []
    process_id = getattr(entry, "process_id", None)
    if process_id is not None:
        pids.append(_require_pid(process_id, "process_id"))
    for extra in extra_pids:
        pids.append(_require_pid(extra, "process_id"))
    for process_id in dict.fromkeys(pids):
        if process_id == runner_pid:
            logger.warning(
                "Task task_id: %s process_id %s is the runner; not killing it",
                entry.id,
                process_id,
            )
            continue
        _kill_process_tree(process_id)


async def _mark_failed_if_active(entry: NodeRegistry, error: str) -> bool:
    if not error.strip():
        raise ValueError("error message is required")
    completed_at = datetime.now()
    collection = context.db.get_collection(NodeRegistry)
    updated = await collection.find_one_and_update(
        {
            "_id": entry.id,
            "status": {"$in": [status.value for status in _ACTIVE_STATUSES]},
        },
        {
            "$set": {
                "status": TaskStatus.FAILED.value,
                "error": error,
                "completed_at": completed_at,
            }
        },
    )
    if updated is None:
        return False
    entry.status = TaskStatus.FAILED
    entry.error = error
    entry.completed_at = completed_at
    return True


async def fail_and_terminate_task(
    entry: NodeRegistry,
    *,
    error: str,
    runner_pid: int,
    extra_pids: tuple[int, ...] = (),
    seen: set[ObjectId] | None = None,
) -> bool:
    """Kill an active task, mark it failed, then do the same for its children."""
    if entry is None or entry.id is None:
        raise ValueError("task registry entry is required")
    if not error.strip():
        raise ValueError("error message is required")
    fresh = await context.db.load_task_by_id(entry.id)
    if fresh is None:
        raise ValueError(f"Task task_id: {entry.id} could not be reloaded")
    if fresh.status not in _ACTIVE_STATUSES:
        return False
    if seen is None:
        seen = set()
    if fresh.id in seen:
        return False
    seen.add(fresh.id)
    logger.info(
        "Task task_id: %s terminating with %s",
        fresh.id,
        error,
    )
    await _stop_task(fresh, runner_pid=runner_pid, extra_pids=extra_pids)
    if not await _mark_failed_if_active(fresh, error):
        return False
    entry.status = TaskStatus.FAILED
    entry.error = error
    entry.completed_at = fresh.completed_at
    await fail_and_terminate_children(fresh.id, runner_pid=runner_pid, seen=seen)
    return True


async def fail_and_terminate_children(
    parent_id: object,
    *,
    runner_pid: int,
    seen: set[ObjectId] | None = None,
) -> None:
    """Fail and stop tasks whose parent_ids include this parent."""
    parent = _parent_object_id(parent_id)
    error = f"Terminated because parent task {parent} failed"
    children = await context.db.find(NodeRegistry, NodeRegistry.parent_ids.in_([parent]))
    if children is None:
        raise ValueError(f"child lookup for task_id: {parent} returned None")
    for child in children:
        await fail_and_terminate_task(
            child,
            error=error,
            runner_pid=runner_pid,
            seen=seen,
        )


async def fail_abandoned_child(
    entry: NodeRegistry,
    *,
    parent_id: object,
    runner_pid: int,
    extra_pids: tuple[int, ...] = (),
) -> None:
    """Fail a child whose parent stopped waiting. Logs and swallows termination errors."""
    if parent_id is None:
        error = "Terminated because the parent task failed"
    else:
        error = f"Terminated because parent task {parent_id} failed"
    try:
        await fail_and_terminate_task(
            entry,
            error=error,
            runner_pid=runner_pid,
            extra_pids=extra_pids,
        )
    except Exception:
        logger.exception(
            "Task task_id: %s could not be terminated after the parent failed",
            getattr(entry, "id", None),
        )
