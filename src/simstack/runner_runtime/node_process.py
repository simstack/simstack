"""Launch an isolated scientific Python process and acknowledge its outcome."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Union

from simstack.core.context import GlobalState
from simstack.core.definitions import TaskStatus
from simstack.core.run_node_protocol import RESULT_PREFIX, parse_run_node_result
from simstack.core.simstack_result import SimstackResult
from simstack.models import NodeRegistry
from simstack.models.document import Model
from simstack.util.sanitized_output import sanitized_command, sanitized_tail

if TYPE_CHECKING:
    from simstack.core.node import Node

logger = logging.getLogger("Node")


async def record_process_failure(
    node: Node, context: GlobalState, launch_entry: NodeRegistry, error: str, *, returncode: int | None = None,
    pending=None,
) -> None:
    """Fallback is fenced by the original launch, never by a freshly read one."""
    termination = {
        "exit_code": returncode if returncode is not None and returncode >= 0 else None,
        "signal": -returncode if returncode is not None and returncode < 0 else None,
    }
    try:
        updated = await context.server.tasks.fail_attempt(
            launch_entry, error, phase="subprocess", **termination,
        )
        if updated is not None and node.registry_entry is not None:
            node.registry_entry = updated
    except Exception as exc:
        logger.warning("Could not record subprocess failure for task %s", launch_entry.id, exc_info=True)
        if pending is not None:
            return  # The original physical receipt is retained by the caller.
        from simstack.server_api.http_transport import RunnerAPIError
        if isinstance(exc, (RunnerAPIError, OSError)):
            from simstack.runner_runtime.pending_execution_failures import (
                record_failure,
            )
            try:
                record_failure(error, phase="subprocess", execution=context.server.tasks.execution(launch_entry),
                    server=context.server, resource=getattr(launch_entry, "_execution_resource", None),
                    rejected_status=getattr(exc, "status_code", None), **termination)
            except OSError:
                logger.warning("Could not journal subprocess failure for task %s", launch_entry.id, exc_info=True)


async def _confirm_owned_process_exit(context: GlobalState, launch_entry, process, *, error=None):
    """A surviving launcher can ACK the exact controller it has drained."""
    if (process is None or process.returncode is None
            or not getattr(launch_entry, "_execution_local", False)):
        return
    from simstack.core.services.task_termination import stop_task_execution
    from simstack.server_api.http_transport import RunnerAPIError
    while True:
        try:
            current = await context.server.tasks.get_task(launch_entry.id)
            if (current is None or context.server.tasks.execution(current) != context.server.tasks.execution(launch_entry)
                    or (getattr(current, "_execution_owner", None) or {}).get("process_id") != process.pid):
                return
            terminal = current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIME_OUT}
            if not terminal and (error is None or current.status == TaskStatus.TERMINATING):
                return
            await stop_task_execution(current, server=context.server, runner_pid=os.getpid(),
                workdir=context.config.workdir, local_process=process)
            if not terminal:
                current = await context.server.tasks.fail_attempt(launch_entry, error, phase="subprocess",
                    exit_code=process.returncode if process.returncode >= 0 else None,
                    signal=-process.returncode if process.returncode < 0 else None)
            await context.server.tasks.termination_confirmed(
                launch_entry, current.error or "Scientific execution stopped")
            observation = getattr(launch_entry, "_native_observation", None)
            if observation is not None:
                from simstack.runner_runtime.native_observations import retire
                try:
                    retire(context.server, observation)
                except OSError:
                    logger.warning("Task %s retains an acknowledged native observation", launch_entry.id, exc_info=True)
            return
        except RunnerAPIError as exc:
            if exc.status_code in {401, 403, 404, 409}:
                return  # A replacement execution never receives the old ACK.
            if exc.status_code is not None and exc.status_code < 500:
                raise
            logger.warning("Task %s awaits its owned-process stop acknowledgement", launch_entry.id, exc_info=True)
            await asyncio.sleep(1)


async def _spawn_child(*command, server, job_entry, ownership, **options):
    """Start the inline worker with the same ownership as its admitted attempt."""
    if os.name == "nt" and job_entry is not None:
        from simstack.runner_runtime.windows_science_job import job_name, spawn_job_process
        return spawn_job_process(command, name=job_name(server, job_entry),
            startupinfo=ownership.get("startupinfo"), retain_in_child=True, **options)
    return await asyncio.create_subprocess_exec(
        *command, start_new_session=os.name != "nt", **options, **ownership
    )


async def run_node_as_process(
    node: Node, context: GlobalState,
) -> Union[Model, SimstackResult, bool, None]:
    """Run this task in an isolated child and restore its persisted result."""
    from simstack.runner_runtime.execution_environment import (
        task_environment,
        task_launch_command,
        task_project_root,
    )

    assert node.registry_entry is not None
    launch_entry = node.registry_entry
    from simstack.runner_runtime.launch_claims import current_execution, task_execution
    # Allocate once for this actual process; the environment and fallback
    # keep this identity even after a canonical task refresh.
    with task_execution(launch_entry):
        execution = current_execution()
        if execution is None:
            raise RuntimeError("A subprocess launch requires its claim identity")
        object.__setattr__(launch_entry, "_execution_start_id", execution["start_id"])

    resource = (
        str(context.config.resource)
        if node.parameters.resource == "self"
        else str(node.parameters.resource)
    )
    project_root = str(task_project_root(launch_entry, context.config.project_root))
    cmd = task_launch_command(launch_entry, resource, project_root, in_docker=context.in_docker,
        local_user=getattr(launch_entry, "_execution_local", False))

    clean_cmd = sanitized_command(cmd, None)
    python_path_entries = [project_root]
    if not context.in_docker:
        python_path_entries.extend(
            str(path)
            for path in (getattr(context.config, "python_paths", None) or [])
        )
    inherited_python_path = os.environ.get("PYTHONPATH")
    if inherited_python_path:
        python_path_entries.append(inherited_python_path)
    child_python_path = os.pathsep.join(
        dict.fromkeys(path for path in python_path_entries if path)
    )

    logger.info(
        "Task task_id: %s spawning run_node subprocess: %s",
        node.id,
        " ".join(clean_cmd),
    )

    proc = None
    try:
        # A shared child may outlive this launcher. Regular files do not
        # break its output when the parent process closes its read handles.
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            from simstack.runner_runtime.launch_claims import inherit_launch_reservation
            from simstack.runner_runtime.native_observations import observe_local_launch
            environment = {**task_environment(node.registry_entry, server=context.server), "PYTHONPATH": child_python_path}
            with (inherit_launch_reservation(getattr(node, "_inline_launch_reservation", None), environment) as ownership,
                  observe_local_launch(context.server, launch_entry, environment, ownership,
                      enabled=getattr(launch_entry, "_execution_local", False)
                          and not node.parameters.in_docker and not context.in_docker) as ownership):
                from contextlib import nullcontext
                job_context = nullcontext((None, ownership))
                creationflags = 0
                if os.name == "nt":
                    from simstack.runner_runtime.windows_science_job import (
                        child_creationflags,
                        controller_job,
                    )
                    creationflags = child_creationflags()
                    if getattr(launch_entry, "_execution_local", False) and not node.parameters.in_docker:
                        job_context = controller_job(context.server, launch_entry, environment, ownership)
                with job_context as (science_job, child_ownership):
                    proc = await _spawn_child(
                        *cmd, server=context.server,
                        job_entry=launch_entry if science_job is None and not node.parameters.in_docker else None,
                        ownership=child_ownership, stdout=stdout_file, stderr=stderr_file,
                        cwd=project_root, env=environment, creationflags=creationflags,
                    )
                    if science_job is not None:
                        import weakref
                        proc._simstack_windows_job = science_job
                        weakref.finalize(proc, science_job.close)
            if os.name != "nt" and isinstance(proc, asyncio.subprocess.Process):
                proc._simstack_process_group = True
            from simstack.runner_runtime.launch_claims import record_spawn
            record_spawn(context.server, launch_entry, proc.pid)
            from simstack.core.services.task_termination import track_local_process
            with track_local_process(launch_entry, proc, server=context.server):
                if (getattr(launch_entry, "_execution_local", False)
                        or (getattr(launch_entry, "_execution_launch_kind", None) == "inline"
                            and launch_entry.parameters.queue == "slurm-queue")):
                    from simstack.core.services.task_termination import (
                        monitor_task_process,
                    )
                    stdout_b, stderr_b = await monitor_task_process(launch_entry, proc,
                        server=context.server, runner_pid=os.getpid(), workdir=context.config.workdir)
                else:
                    stdout_b, stderr_b = await proc.communicate()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout_b, stderr_b = stdout_file.read(), stderr_file.read()
    except asyncio.CancelledError:
        # TaskGroup cancellation is a BaseException, so the handler below
        # never runs. Kill this child or its row stays RUNNING.
        from simstack.core.services.fail_children import fail_abandoned_child

        pids: tuple[int, ...] = ()
        if proc is not None and proc.returncode is None:
            pid = getattr(proc, "pid", None)
            if type(pid) is int and pid > 0:
                pids = (pid,)
            elif pid is not None:
                logger.error(
                    "Task task_id: %s process_id must be a positive int, got %r",
                    node.id,
                    pid,
                )
        from simstack.core.context import execution_server
        with execution_server(getattr(node, "_dependency_server", None)):
            await fail_abandoned_child(
                node.registry_entry,
                parent_id=node.parent_id,
                parent_execution=node._parent_execution,
                runner_pid=os.getpid(),
                extra_pids=pids,
                local_process=proc,
            )
        # stop_task_execution already waits for an owned process it stops.
        # A shared child keeps running for its other consumers.
        raise
    except Exception as exc:
        error = sanitized_tail(str(exc), None)
        if proc is not None and (proc.returncode is None or (getattr(proc, "_simstack_process_group", False) or getattr(proc, "_simstack_windows_job", None))):
            from simstack.core.services.task_termination import stop_task_execution
            await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                workdir=context.config.workdir, local_process=proc)
        if proc is None or proc.returncode is not None:
            await record_process_failure(node, context, launch_entry, error, returncode=proc.returncode if proc is not None else None)
            await _confirm_owned_process_exit(context, launch_entry, proc, error=error)
        if proc is None and isinstance(exc, OSError):
            # exec/CreateProcess failed without returning a child handle.
            observation = getattr(launch_entry, "_native_observation", None)
            if observation is not None:
                from simstack.runner_runtime.native_observations import retire
                try:
                    retire(context.server, observation)
                except OSError:
                    logger.warning("Task %s retains its failed-spawn observation", launch_entry.id, exc_info=True)
        raise RuntimeError(
            f"Task task_id: {node.id} could not start run_node child: {error}"
        ) from None
    return await _reconcile_child_exit(
        node, context, launch_entry, proc, resource=resource, execution=execution,
        stdout=sanitized_tail(stdout_b, None), stderr=sanitized_tail(stderr_b, None),
    )


async def _reconcile_child_exit(
    node: Node, context: GlobalState, launch_entry: NodeRegistry, proc, *,
    resource: str, execution: dict, stdout: str, stderr: str,
) -> Union[Model, SimstackResult, bool, None]:
    """Deliver physical exit evidence and restore the result committed by Server."""
    outcome = parse_run_node_result(stdout)
    pending = None
    if proc.returncode:
        from simstack.runner_runtime.pending_execution_failures import (
            cleanup_scope,
            record_observed_failure,
        )
        cleanup = None
        if getattr(proc, "_simstack_process_group", False) or getattr(proc, "_simstack_windows_job", None):
            cleanup = {"container": node.parameters.in_docker, "scope": cleanup_scope(),
                "process_group": proc.pid if getattr(proc, "_simstack_process_group", False) else None,
                "windows_job": getattr(getattr(proc, "_simstack_windows_job", None), "name", None)}
        pending = record_observed_failure(
            (outcome.error if outcome is not None else None)
            or f"run_node child exited with code {proc.returncode}",
            phase="subprocess", execution=execution, server=context.server, resource=resource,
            return_kind=outcome.return_kind if outcome is not None else "exception",
            exit_code=proc.returncode if proc.returncode >= 0 else None,
            signal=-proc.returncode if proc.returncode < 0 else None,
            cleanup=cleanup,
        )
    try:
        from simstack.core.services.task_termination import (
            TaskTerminationError,
            stop_owned_process_group,
            stop_task_execution,
        )
        from simstack.server_api.http_transport import RunnerAPIError
        while True:
            try:
                if getattr(proc, "_simstack_windows_job", None) is not None:
                    await stop_owned_process_group(proc)
                if proc.returncode != 0 and (getattr(proc, "_simstack_process_group", False) or getattr(proc, "_simstack_windows_job", None)):
                    await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                        workdir=context.config.workdir, local_process=proc)
                break
            except (OSError, TaskTerminationError, RunnerAPIError):
                # Retain the receipt and physical ownership; journal replay
                # cannot terminalize active science until cleanup is proved.
                logger.warning("Task %s retains its inline worker until cleanup is verified", node.id, exc_info=True)
                await asyncio.sleep(1)
        if pending is not None and "cleanup" in pending[1]:
            pending[1].pop("cleanup")
            if pending[0] is not None:
                from simstack.runner_runtime.pending_execution_failures import (
                    record_failure,
                )
                try:
                    record_failure(server=context.server, resource=resource, **pending[1])
                except OSError:
                    # The guarded receipt remains recoverable; a live API
                    # can still retain the now-verified physical evidence.
                    logger.warning("Task %s retains its guarded exit receipt", node.id, exc_info=True)
                    pending = None, pending[1]

        # Read the original attempt outcome recorded by the subprocess.
        try:
            updated_entry = await context.server.tasks.get_outcome(launch_entry)
        except Exception as exc:
            if proc.returncode == 0 and outcome is not None and outcome.success:
                # Success follows Server's completion acknowledgement. A bool
                # needs no further data; model references still require a read.
                from simstack.server_api.http_transport import RunnerAPIError
                if (outcome.return_kind == "bool" and isinstance(exc, RunnerAPIError)
                        and (exc.status_code is None or exc.status_code >= 500)):
                    node.registry_entry.status = TaskStatus.COMPLETED
                    node.registry_entry.return_kind = "bool"
                    return True
                raise
            logger.warning("Could not read child outcome for task %s", node.id, exc_info=True)
            updated_entry = launch_entry
        if updated_entry is None:
            raise RuntimeError(
                f"Task task_id: {node.id} has no recorded outcome after run_node subprocess"
            )
        if (updated_entry.status == TaskStatus.TERMINATING and proc.returncode is not None
                and (getattr(launch_entry, "_execution_local", False)
                     or getattr(launch_entry, "_execution_launch_kind", None) == "inline")):
            # A dead controller may leave a worker or Docker container alive.
            # Verify physical cleanup before acknowledging the original attempt.
            from simstack.core.services.task_termination import stop_task_execution
            await stop_task_execution(updated_entry, server=context.server, runner_pid=os.getpid(),
                workdir=context.config.workdir, local_process=proc)
            updated_entry = await context.server.tasks.termination_confirmed(
                launch_entry, updated_entry.error or "Terminated by user")
        if (getattr(updated_entry, "_execution_owner", None)
                and updated_entry._execution_owner["process_id"] == proc.pid
                and updated_entry.status != TaskStatus.TERMINATING):
            from simstack.core.services.task_termination import stop_task_execution
            await stop_task_execution(updated_entry, server=context.server, runner_pid=os.getpid(),
                workdir=context.config.workdir, local_process=proc)
        await _confirm_owned_process_exit(context, launch_entry, proc)
        node.registry_entry = updated_entry
        committed_kind = getattr(updated_entry, "return_kind", None)
        committed = (
            updated_entry.status == TaskStatus.COMPLETED
            and not updated_entry.error
            and committed_kind in {"bool", "model", "multiple", "none"}
        )
        if committed and (proc.returncode != 0 or outcome is None or not outcome.success):
            logger.warning("Task %s completed before child launcher exited with code %s", node.id, proc.returncode)

        if not committed and (proc.returncode != 0 or outcome is None or not outcome.success):
            child_error = node.registry_entry.error or (
                outcome.error if outcome is not None else None
            )
            details = []
            visible_stdout = "\n".join(
                line for line in stdout.splitlines() if not line.startswith(RESULT_PREFIX)
            ).strip()
            if visible_stdout:
                details.append(f"stdout:\n{visible_stdout}")
            if stderr:
                details.append(f"stderr:\n{stderr}")
            detail_tail = sanitized_tail(
                "\n".join(detail for detail in details if detail),
                None,
                limit=3500,
            )
            error = sanitized_tail(child_error, None, limit=None) if child_error else f"run_node child exited with code {proc.returncode}"
            if node.registry_entry.status in {TaskStatus.SUBMITTED, TaskStatus.RETRIEVED, TaskStatus.RUNNING,
                    TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING}:
                await record_process_failure(node, context, launch_entry, error, returncode=proc.returncode, pending=pending)
                await _confirm_owned_process_exit(context, launch_entry, proc, error=error)
            if detail_tail:
                logger.warning("Task %s child diagnostics: %s", node.id, detail_tail)
            raise RuntimeError(f"Task task_id: {node.id} {error}")

        if node.registry_entry.status != TaskStatus.COMPLETED:
            error = (
                f"run_node child reported success but task status is "
                f"{node.registry_entry.status}"
            )
            if node.registry_entry.status in {TaskStatus.SUBMITTED, TaskStatus.RETRIEVED, TaskStatus.RUNNING,
                    TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING}:
                await record_process_failure(node, context, launch_entry, error, returncode=proc.returncode, pending=pending)
                await _confirm_owned_process_exit(context, launch_entry, proc, error=error)
            raise RuntimeError(f"Task task_id: {node.id} {error}")

        if stderr:
            logger.warning("Task task_id: %s run_node stderr: %s", node.id, stderr)
        return_kind = committed_kind if committed else outcome.return_kind
        if return_kind == "bool":
            return True
        if return_kind in {"model", "multiple"}:
            return await node.load_results(return_kind=return_kind)
        if return_kind == "none":
            return None
        error = (
            f"Task task_id: {node.id} returned unsupported child result kind "
            f"{return_kind!r}"
        )
        await record_process_failure(node, context, launch_entry, error, returncode=proc.returncode, pending=pending)
        raise RuntimeError(error)
    finally:
        if pending is not None:
            # Terminal science keeps its result/primary error; physical
            # evidence belongs to this exact launch, even after a rerun.
            from simstack.core.run_node import _retain_worker_exit
            await _retain_worker_exit(context.server, pending, resource)
