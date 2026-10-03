import asyncio
import logging
import os
import platform
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry

logger = logging.getLogger("NodeRunner")

TERMINATED_BY_USER = "Terminated by user"


async def record_process_id(registry_entry: NodeRegistry, process_id: int, database) -> None:
    """Persist the OS pid of a spawned job process.

    This must be the job process, not the runner that is polling for work.
    """
    if database is None:
        raise ValueError("database is required to persist process_id")
    if registry_entry.id is None:
        raise ValueError("registry entry id is required to persist process_id")
    if type(process_id) is not int or process_id < 1:
        raise ValueError(f"process_id must be a positive int, got {process_id!r}")
    registry_entry.process_id = process_id
    get_collection = getattr(database, "get_collection", None)
    if not callable(get_collection):
        raise ValueError("database cannot persist process_id")
    collection = get_collection(NodeRegistry)
    await collection.update_one(
        {"_id": registry_entry.id},
        {"$set": {"process_id": process_id}},
    )


def _kill_process_tree(process_id: int) -> None:
    """Kill a process and its children. Does not kill the process session."""
    if type(process_id) is not int or process_id < 1:
        raise ValueError(f"process_id must be a positive int, got {process_id!r}")
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


async def terminate_task(registry_entry: NodeRegistry, *, runner_pid: int) -> None:
    """Stop a terminating task and mark it failed.

    Slurm jobs are cancelled with ``scancel``. Docker containers are killed
    from the task cidfile. A recorded job pid is killed with its children.
    The runner process itself is never killed. A task with nothing left
    running is still marked failed.
    """
    if type(runner_pid) is not int or runner_pid < 1:
        raise ValueError(f"runner_pid must be a positive int, got {runner_pid!r}")
    fresh = await context.db.load_task_by_id(registry_entry.id)
    if fresh is None:
        raise ValueError(f"Task task_id: {registry_entry.id} could not be reloaded")
    if fresh.status != TaskStatus.TERMINATING:
        return

    job_id = fresh.job_id
    if isinstance(job_id, str) and job_id.isdigit():
        await _run_command(["scancel", job_id])

    workdir = getattr(context.config, "workdir", None)
    if workdir is None:
        raise ValueError("runner workdir is not configured")
    from simstack.core.run_docker import docker_cidfile_path, read_container_id

    cidfile = docker_cidfile_path(Path(workdir) / fresh.name / str(fresh.id))
    container_id = read_container_id(cidfile) if cidfile.is_file() else None
    if container_id:
        await _run_command(["docker", "kill", container_id])

    process_id = fresh.process_id
    if process_id is not None:
        if type(process_id) is not int or process_id < 1:
            raise ValueError(
                f"Task task_id: {fresh.id} process_id must be a positive int, "
                f"got {process_id!r}"
            )
        if process_id == runner_pid:
            logger.warning(
                "Task task_id: %s process_id %s is the runner; not killing it",
                fresh.id,
                process_id,
            )
        else:
            _kill_process_tree(process_id)

    completed_at = datetime.now()
    collection = context.db.get_collection(NodeRegistry)
    updated = await collection.find_one_and_update(
        {"_id": fresh.id, "status": TaskStatus.TERMINATING.value},
        {
            "$set": {
                "status": TaskStatus.FAILED.value,
                "error": TERMINATED_BY_USER,
                "completed_at": completed_at,
            }
        },
    )
    if updated is None:
        return
    fresh.status = TaskStatus.FAILED
    fresh.error = TERMINATED_BY_USER
    fresh.completed_at = completed_at
    registry_entry.status = TaskStatus.FAILED
    registry_entry.error = TERMINATED_BY_USER
    registry_entry.completed_at = completed_at
