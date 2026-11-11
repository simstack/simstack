import asyncio
import logging
import os
import platform
import signal
import subprocess
from pathlib import Path
from contextlib import contextmanager

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry

logger = logging.getLogger("NodeRunner")

TERMINATED_BY_USER = "Terminated by user"


class TaskTerminationError(RuntimeError):
    """A task could not be confirmed stopped."""


_local_processes = {}


def _launch_key(entry):
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    execution = WorkflowTasksAPI.execution(entry)
    return tuple(execution[key] for key in ("task_id", "generation", "claim_id", "start_id"))


@contextmanager
def track_local_process(entry, process, *, server=None):
    """Keep physical ownership only while this launcher's handle is live."""
    key = _launch_key(entry)
    if server is not None:
        process._simstack_execution_server = server
    _local_processes[key] = process
    try:
        yield
    finally:
        _local_processes.pop(key, None)


def owned_local_process(entry):
    """A task ID or resource name alone cannot authorize process termination."""
    return _local_processes.get(_launch_key(entry))


async def record_process_id(registry_entry: NodeRegistry, process_id: int, api) -> None:
    """Report a spawned job PID without saving its task document."""
    if registry_entry.id is None:
        raise ValueError("task identity is required")
    if type(process_id) is not int or process_id < 1:
        raise ValueError(f"process_id must be a positive int, got {process_id!r}")
    backend = getattr(registry_entry, "_execution_container_backend", None)
    await api.tasks.record_process(registry_entry, process_id,
        **({"container_backend": backend} if backend is not None else {}))
    registry_entry.process_id = process_id


def _kill_process_tree(process_id: int, protected_pids=frozenset()) -> None:
    """Kill a process and its children. Does not kill the process session."""
    if type(process_id) is not int or process_id < 1:
        raise ValueError(f"process_id must be a positive int, got {process_id!r}")
    if process_id in protected_pids:
        return
    if platform.system() == "Windows" and not protected_pids:
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    for child_pid in _child_pids(process_id):
        _kill_process_tree(child_pid, protected_pids)
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/PID", str(process_id), "/F"], capture_output=True, check=False)
        return
    try:
        os.kill(process_id, getattr(signal, "SIGKILL", 9))
    except ProcessLookupError:
        return
    except OSError as exc:
        logger.warning("Could not kill process %s: %s", process_id, exc)


def _child_pids(process_id: int) -> list[int]:
    if os.name == "posix" and Path("/proc/self/stat").is_file():
        from simstack.runner_runtime.process_table import process_rows
        return [pid for pid, parent, _, _ in process_rows() if parent == process_id]
    try:
        command = (["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object ParentProcessId -eq " + str(process_id)
                    + " | ForEach-Object ProcessId"] if platform.system() == "Windows"
                   else ["ps", "-axo", "pid=,ppid="])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise TaskTerminationError(f"Could not list children of {process_id}") from exc
    if result.returncode != 0:
        raise TaskTerminationError(f"Could not list children of {process_id}")
    if platform.system() == "Windows":
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if any(not value.isdigit() or int(value) < 1 for value in values):
            raise TaskTerminationError("Unexpected response while listing process children")
        return [int(value) for value in values]
    children: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(field.isdigit() for field in fields) and int(fields[1]) == process_id:
            children.append(int(fields[0]))
    return children


async def _run_command(args: list[str]) -> str:
    try:
        from simstack.util.slurm_observation import query_environment
        process = await asyncio.create_subprocess_exec(
            *args,
            **({"env": query_environment()} if args[0] in {"squeue", "sacct"} else {}),
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


async def shared_launch_process(child, parent, server):
    """Identify the original local launch before allowing its parent to stop."""
    from simstack.runner_runtime.launch_claims import _actor, receipt

    parent_execution = server.tasks.execution(parent)
    if (getattr(child, "_execution_parent", None) != parent_execution
            or getattr(child, "_execution_launch_kind", None) != "inline"
            or getattr(child, "_execution_resource", None) != getattr(parent, "_execution_resource", None)):
        # A cached task launched by another process/resource is not in this tree.
        return None
    original = server.tasks.execution(child)
    actor = _actor(server)
    while child.status in {TaskStatus.SUBMITTED, TaskStatus.RETRIEVED, TaskStatus.RUNNING,
                           TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING, TaskStatus.TERMINATING}:
        owner = getattr(child, "_execution_owner", None)
        if owner:
            if owner["execution"] != original:
                raise TaskTerminationError("Physical owner differs from its original local launch")
            return owner["process_id"]
        if (not child.parameters.in_docker and child.process_id is not None
                and child._execution_start_id is not None):
            return child.process_id
        try:
            intent = receipt(server, child.id)
        except (OSError, ValueError) as exc:
            raise TaskTerminationError("Could not read the shared child launch receipt") from exc
        if intent is not None:
            expected_actor = {name: value for name, value in actor.items() if name != "credential_fingerprint"}
            recorded_actor = {name: value for name, value in intent.get("actor", {}).items() if name != "credential_fingerprint"}
            if (recorded_actor != expected_actor
                    or any(intent.get(name) != original.get(name) for name in ("generation", "claim_id"))
                    or intent.get("publication", {}).get("parent_execution") != parent_execution
                    or child._execution_start_id not in {None, intent.get("start_id")}):
                raise TaskTerminationError("Cannot verify the original shared child launch")
            if intent.get("process_id") is not None:
                return intent["process_id"]
        if child.parameters.in_docker and child.status in {TaskStatus.RUNNING, TaskStatus.SLURM_RUNNING}:
            # The daemon-owned container has started; its PID is not a host PID.
            return None
        # The publisher remains alive until its queued/preparing child has a
        # physical handle. Killing it now would leave a shared claim unlaunchable.
        await asyncio.sleep(.05)
        current = await server.tasks.get_task(child.id)
        if current is None or any(server.tasks.execution(current).get(name) != original.get(name)
                                  for name in ("task_id", "generation", "claim_id")):
            return None
        if (original.get("start_id") is not None
                and current._execution_start_id not in {None, original["start_id"]}):
            return None
        child = current
    return None


async def protected_child_processes(entry, server):
    """Ask Server to detach stopped consumers before killing a physical tree."""
    active = {TaskStatus.SUBMITTED, TaskStatus.RETRIEVED, TaskStatus.RUNNING,
              TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING}
    pending, parents, children = [entry], {}, {}
    while pending:
        parent = pending.pop()
        if str(parent.id) in parents:
            continue
        parents[str(parent.id)] = parent
        for child in await server.tasks.children(parent):
            cancelled = await server.tasks.cancel_child(parent.id,
                server.tasks.execution(parent), child, entry.error or TERMINATED_BY_USER)
            if cancelled is None:
                continue  # Server retired the observed generation; do not touch its replacement.
            children[str(cancelled.id)] = cancelled
            if (cancelled.status not in active
                    and getattr(cancelled, "_execution_parent", None) == server.tasks.execution(parent)):
                # A reused subtree can belong to another local root. Its owner
                # observes cancellation; this launcher cannot mutate its children.
                pending.append(cancelled)
    protected = set()
    for child in children.values():
        if child.status not in active and not (child.status == TaskStatus.TERMINATING and getattr(child, "_execution_owner", None)):
            continue
        # Detach every stopped consumer before deciding which launches survive.
        # A reused dependency may be reached before its original launch parent.
        parent = parents.get((getattr(child, "_execution_parent", None) or {}).get("task_id"))
        if parent is None:
            continue
        process_id = await shared_launch_process(child, parent, server)
        if process_id is not None:
            if type(process_id) is not int or process_id < 1:
                raise TaskTerminationError("Shared child launch has an invalid process identity")
            protected.add(process_id)
    return protected


async def monitor_task_process(entry, process, *, server, workdir, runner_pid):
    """Retain the owned process until it exits or its exact attempt is stopped."""
    from simstack.server_api.http_transport import RunnerAPIError

    execution = server.tasks.execution(entry)
    communication = asyncio.create_task(process.communicate())
    stopped_reason = None
    try:
        while not communication.done() or stopped_reason is not None:
            if stopped_reason is not None:
                try:
                    await server.tasks.termination_confirmed(entry, stopped_reason)
                    stopped_reason = None
                except RunnerAPIError as exc:
                    if exc.status_code is not None and exc.status_code < 500:
                        break  # A superseded attempt cannot acknowledge its replacement.
                    logger.warning("Task %s awaits its stopped-process acknowledgement", entry.id, exc_info=True)
                    await asyncio.sleep(1)
                    continue
                if communication.done():
                    break
            done, _ = await asyncio.wait({communication}, timeout=1)
            if done:
                break
            try:
                current = await server.tasks.get_outcome(entry)
                if current is None:
                    continue
                observed = server.tasks.execution(current)
                if (any(observed.get(key) != execution.get(key) for key in ("task_id", "generation", "claim_id"))
                        or observed.get("start_id") not in {None, execution.get("start_id")}):
                    continue
                if observed.get("start_id") is not None and os.environ.get("SIMSTACK_LAUNCH_RESERVATION"):
                    from simstack.runner_runtime.launch_claims import acknowledge_start
                    acknowledge_start(server, entry, current)
                scheduler_stop = False
                if getattr(process, "_simstack_in_container", False) and os.environ.get("SIMSTACK_SLURM_CONTAINER_EXECUTION") == "1":
                    control = await server.slurm.execution_control(entry)
                    scheduler_stop = control["stop_requested"]
                if current.status == TaskStatus.TERMINATING or scheduler_stop:
                    await stop_task_execution(current, server=server, runner_pid=runner_pid,
                        workdir=workdir, local_process=process)
                    stopped_reason = current.error or (control.get("reason") if scheduler_stop else None) or TERMINATED_BY_USER
            except (RunnerAPIError, TaskTerminationError):
                logger.warning("Task %s awaits verified process cancellation", entry.id, exc_info=True)
        return await communication
    except Exception:
        # Unexpected observation/decoding errors are infrastructure failures,
        # but cannot turn a still-running owned worker into a terminal task.
        while process.returncode is None:
            try:
                await stop_task_execution(entry, server=server, runner_pid=runner_pid,
                    workdir=workdir, local_process=process)
            except Exception:
                logger.warning("Task %s retains its worker until physical cleanup is verified", entry.id, exc_info=True)
                await asyncio.sleep(1)
        raise
    finally:
        if not communication.done():
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)


async def stop_task_execution(entry, *, server, runner_pid: int, workdir, extra_pids=(), local_process=None) -> None:
    """Stop the scheduler job, or use the existing local cleanup path."""
    if type(runner_pid) is not int or runner_pid < 1:
        raise ValueError("runner_pid must be a positive int")
    if getattr(entry, "_execution_local", False) and local_process is None:
        raise TaskTerminationError("A local user execution must be stopped by its owning launcher")
    container_owner = getattr(entry, "_execution_container_owner", None)
    backend = getattr(entry, "_execution_container_backend", None)
    in_docker = bool(container_owner or backend or getattr(getattr(entry, "parameters", None), "in_docker", False))
    owner = getattr(entry, "_execution_owner", None)
    if owner and local_process is not None and local_process.pid == owner["process_id"]:
        # A caller may stop its owned session or exact container directly; it
        # must not wait forever for an unresponsive controller to acknowledge.
        if (not getattr(local_process, "_simstack_process_group", False)
                and not getattr(local_process, "_simstack_windows_job", None) and not in_docker):
            await local_process.wait()
            recorded = await server.tasks.get_outcome(entry)
            if recorded is not None and recorded.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIME_OUT}:
                return
            raise TaskTerminationError("The controller exited without verifiable scientific process ownership")
    job_id = entry.job_id
    queue = getattr(getattr(entry, "parameters", None), "queue", None)
    queue = getattr(queue, "value", queue)
    scheduler_owner = getattr(entry, "_execution_scheduler_owner", None)
    if scheduler_owner:
        if scheduler_owner["execution"] != server.tasks.execution(entry):
            raise TaskTerminationError("Slurm ownership differs from this execution")
        job_id, queue = scheduler_owner["job_id"], "slurm-queue"
    if container_owner:
        job_id, queue = None, "default"  # Admission, rather than editable fields.
    if local_process is None and (getattr(entry, "_execution_allocation", None)
            or getattr(entry, "_execution_launch_kind", None) == "inline" and queue == "slurm-queue"):
        raise TaskTerminationError("An inline Slurm child must be stopped by its launching process")
    if local_process is None and (job_id is not None or queue == "slurm-queue"):
        if not isinstance(job_id, str) or not job_id.isdigit():
            raise TaskTerminationError("Cannot stop a Slurm task without a valid job ID")
        if await _slurm_job_present(job_id):
            await _run_command(["scancel", job_id])
            if await _slurm_job_present(job_id):
                raise TaskTerminationError(f"Slurm job {job_id} is still stopping")
        # Docker admission belongs to this incarnation, even when parameters
        # were edited for a later run. Queue absence proves only allocation stop.
        control = await server.slurm.execution_control(entry)
        if (control["container_started"] and control.get("container_backend") != "apptainer"
                and not control["execution_stopped"]):
            raise TaskTerminationError("Slurm container awaits its physical stop acknowledgement")
        # Compute-node PIDs and container IDs are not local Runner handles.
        return

    if in_docker and not getattr(local_process, "_simstack_in_container", False):
        if workdir is None:
            raise ValueError("runner workdir is not configured")
        from simstack.core.run_docker import docker_cidfile_path, docker_daemon_identity, docker_execution_name, inspect_docker_execution
        from simstack.runner_runtime.launch_claims import receipt

        execution = server.tasks.execution(entry)
        if backend == "apptainer" and local_process is None:
            # Its independent supervisor owns the scientific process/session.
            # A stored launcher PID is not proof that its lifetime still exists.
            raise TaskTerminationError("Apptainer cancellation awaits its owning supervisor")
        intent = receipt(server, entry.id)
        same_claim = intent is not None and all(intent.get(field) == execution.get(field)
                                               for field in ("generation", "claim_id"))
        if execution["start_id"] is None and same_claim and intent.get("container_name"):
            execution["start_id"] = intent["start_id"]
        cidfile = docker_cidfile_path(Path(workdir) / entry.name / str(entry.id))
        verified_container = False
        if backend != "apptainer" and execution["start_id"] is not None and (backend == "docker" or cidfile.is_file() or same_claim and intent.get("container_name")
                or owner and local_process is not None and local_process.pid == owner["process_id"]):
            name = docker_execution_name(server, execution)
            try:
                daemon_matches = False
                if container_owner and same_claim and intent.get("container_daemon"):
                    daemon_matches = await asyncio.to_thread(docker_daemon_identity) == intent["container_daemon"]
                    if not daemon_matches:
                        raise TaskTerminationError("Container belongs to a different Docker daemon")
                observed = await asyncio.to_thread(inspect_docker_execution, name)
                if container_owner and observed is None and not daemon_matches:
                    raise TaskTerminationError("Docker absence requires its original daemon observation")
            except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
                raise TaskTerminationError("Cannot verify this task's Docker execution") from exc
            if observed is not None:
                status = observed["State"]["Status"]
                if status == "created":
                    await _run_command(["docker", "rm", observed["Id"]])
                elif status not in {"exited", "dead"}:
                    await _run_command(["docker", "kill", observed["Id"]])
                try:
                    remaining = await asyncio.to_thread(inspect_docker_execution, name)
                except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
                    raise TaskTerminationError("Docker stop is not yet verified") from exc
                if remaining is not None and remaining["State"]["Status"] not in {"exited", "dead"}:
                    raise TaskTerminationError("The Docker execution is still stopping")
            verified_container = True
        if local_process is None:
            if not verified_container and (entry.process_id is not None or execution["start_id"] is not None):
                raise TaskTerminationError("Container cancellation awaits its owning supervisor or exact Docker identity")
            # Docker daemon evidence owns the container; an old CLI PID may
            # already belong to an unrelated process after Runner restart.
            return
    pids = ([local_process.pid] if local_process is not None and local_process.returncode is None
            else [] if local_process is not None
            else [pid for pid in (entry.process_id, *extra_pids) if pid is not None])
    protected = await protected_child_processes(entry, server) if any(pid != runner_pid for pid in pids) else set()
    if local_process is not None and getattr(local_process, "_simstack_windows_job", None) is not None:
        # Shared SDK children have their own Jobs. An exact Job handle drains
        # science without relying on a PID tree, which can lose or misidentify
        # descendants after controller death.
        await stop_owned_process_group(local_process)
        if local_process.returncode is None:
            local_process.kill()  # A local controller itself is outside its science Job.
        await local_process.wait()
        return
    for process_id in dict.fromkeys(pids):
        if type(process_id) is not int or process_id < 1:
            raise ValueError("process_id must be a positive int")
        if process_id == runner_pid:
            logger.warning("Task task_id: %s process_id is the Runner; not killing it", entry.id)
        else:
            if protected:
                _kill_process_tree(process_id, protected)
            else:
                _kill_process_tree(process_id)
    if local_process is not None:
        if local_process.returncode is None:
            await local_process.wait()
        await stop_owned_process_group(local_process)


async def stop_owned_process_group(process):
    """Verify an owned Job/session, including children orphaned by process death."""
    windows_job = getattr(process, "_simstack_windows_job", None)
    if windows_job is not None:
        await windows_job.stop()
        return
    whole_group = getattr(process, "_simstack_process_group", False)
    group = process.pid if whole_group else getattr(process, "_simstack_science_group", None)
    if group is None:
        return
    if not whole_group and (group != os.getpid() or os.getpgrp() != group):
        raise TaskTerminationError("Scientific process group is not owned by this controller")
    while True:
        from simstack.runner_runtime.process_table import process_rows
        try:
            rows = await asyncio.to_thread(process_rows)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise TaskTerminationError("Cannot verify the stopped scientific process group") from exc
        members = [pid for pid, _, pgid, state in rows
                   if pgid == group and not state.startswith("Z")
                   and (whole_group or pid != os.getpid())]
        if not members:
            return
        if whole_group:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            # The controller must survive to publish the stopped outcome. Its
            # nested SDK children own other sessions, so they remain independent.
            for pid in members:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await asyncio.sleep(.05)


async def terminate_task(registry_entry: NodeRegistry, *, runner_pid: int) -> None:
    """Stop a terminating task and mark it failed.

    Slurm jobs are cancelled with ``scancel``. Docker containers are killed
    from the task cidfile. A recorded job pid is killed with its children.
    The runner process itself is never killed. A task with nothing left
    running is still marked failed.
    """
    if type(runner_pid) is not int or runner_pid < 1:
        raise ValueError(f"runner_pid must be a positive int, got {runner_pid!r}")
    fresh = await context.server.tasks.get_task(registry_entry.id)
    if fresh is None:
        raise ValueError(f"Task task_id: {registry_entry.id} could not be reloaded")
    if fresh.status != TaskStatus.TERMINATING:
        return
    for name in ("_execution_generation", "_execution_claim_id", "_execution_start_id"):
        expected = getattr(registry_entry, name, None)
        if expected is not None and expected != getattr(fresh, name, None):
            return

    await stop_task_execution(
        fresh, server=context.server, runner_pid=runner_pid, workdir=getattr(context.config, "workdir", None)
    )

    updated = await context.server.tasks.termination_confirmed(
        fresh, fresh.error or TERMINATED_BY_USER,
    )
    if updated is None:
        return
    registry_entry.status = updated.status
    registry_entry.error = updated.error
    registry_entry.completed_at = updated.completed_at
    from simstack.core.services.fail_children import fail_and_terminate_children
    await fail_and_terminate_children(fresh, runner_pid=runner_pid)
