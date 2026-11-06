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


class TaskTerminationError(RuntimeError):
    """A task could not be confirmed stopped."""


def stop_mutation_values(entry, values, database):
    from simstack.core.definitions import DBType

    if getattr(database, "databae_type", None) == DBType.API:
        return {**values, "generation": getattr(entry, "_execution_generation", None)}
    return values


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
    from simstack.runner_api.claims import task_execution
    with task_execution(registry_entry):
        await database.task_mutation(registry_entry.id, "process", {"process_id": process_id})


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


async def _run_command(args: list[str]) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise TaskTerminationError(f"{args[0]} timed out while stopping the task") from exc
    except OSError as exc:
        raise TaskTerminationError(f"Could not run {args[0]}: {exc}") from exc
    if process.returncode != 0:
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        raise TaskTerminationError(f"{args[0]} exited {process.returncode}: {stderr}")
    return stdout_b.decode("utf-8", errors="replace").strip()


async def _slurm_job_present(job_id: str) -> bool:
    # A job-specific squeue query may fail when the job has already vanished.
    # %F reports the numeric base ID for both ordinary jobs and array elements.
    output = await _run_command([
        "squeue", "--noheader", "--me", "--all", "--local", "--states=all", "--format=%F|%T",
    ])
    terminal = {"COMPLETED", "CANCELLED", "FAILED", "TIMEOUT", "NODE_FAIL",
                "BOOT_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "DEADLINE"}
    for line in output.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 2 or not fields[0].isdigit() or not fields[1]:
            raise TaskTerminationError("Unexpected response while checking Slurm cancellation")
        if fields[0] == job_id and fields[1].upper() not in terminal:
            return True
    return False


async def stop_task_execution(entry, *, runner_pid: int, workdir, extra_pids=()) -> None:
    """Stop the scheduler job, or use the existing local cleanup path."""
    if type(runner_pid) is not int or runner_pid < 1:
        raise ValueError("runner_pid must be a positive int")
    job_id = entry.job_id
    queue = getattr(getattr(entry, "parameters", None), "queue", None)
    queue = getattr(queue, "value", queue)
    if job_id is not None or queue == "slurm-queue":
        if not isinstance(job_id, str) or not job_id.isdigit():
            raise TaskTerminationError("Cannot stop a Slurm task without a valid job ID")
        if not await _slurm_job_present(job_id):
            return
        await _run_command(["scancel", job_id])
        if await _slurm_job_present(job_id):
            raise TaskTerminationError(f"Slurm job {job_id} is still stopping")
        # Compute-node PIDs and container IDs are not local Runner handles.
        return

    if workdir is None:
        raise ValueError("runner workdir is not configured")
    from simstack.core.run_docker import docker_cidfile_path, read_container_id

    cidfile = docker_cidfile_path(Path(workdir) / entry.name / str(entry.id))
    container_id = read_container_id(cidfile) if cidfile.is_file() else None
    if container_id:
        await _run_command(["docker", "kill", container_id])
    pids = [pid for pid in (entry.process_id, *extra_pids) if pid is not None]
    for process_id in dict.fromkeys(pids):
        if type(process_id) is not int or process_id < 1:
            raise ValueError("process_id must be a positive int")
        if process_id == runner_pid:
            logger.warning("Task task_id: %s process_id is the Runner; not killing it", entry.id)
        else:
            _kill_process_tree(process_id)


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

    await stop_task_execution(
        fresh, runner_pid=runner_pid, workdir=getattr(context.config, "workdir", None)
    )

    completed_at = datetime.now()
    updated = await context.db.task_mutation(fresh.id, "terminate", stop_mutation_values(fresh, {
        "status": TaskStatus.FAILED.value, "error": fresh.error or TERMINATED_BY_USER,
        "completed_at": completed_at,
    }, context.db), [TaskStatus.TERMINATING.value])
    if updated is None:
        return
    fresh.status = TaskStatus.FAILED
    fresh.error = fresh.error or TERMINATED_BY_USER
    fresh.completed_at = completed_at
    registry_entry.status = TaskStatus.FAILED
    registry_entry.error = fresh.error
    registry_entry.completed_at = completed_at
    from simstack.core.definitions import DBType
    if getattr(context.db, "databae_type", None) == DBType.API:
        from simstack.core.services.fail_children import fail_and_terminate_children
        await fail_and_terminate_children(fresh.id, runner_pid=runner_pid)
