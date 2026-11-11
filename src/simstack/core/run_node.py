import argparse
import asyncio
import logging
import os
import sys
import tempfile
from contextlib import contextmanager

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.run_docker import CONTAINER_WORKDIR, run_docker_with_outcome
from simstack.core.run_node_protocol import RESULT_PREFIX, RunNodeResult, encode_run_node_result, parse_run_node_result
from simstack.core.services.node_execution_service import (
    run_node_from_registry_with_outcome,
)
from simstack.core.services.task_termination import (
    TERMINATED_BY_USER,
)
from simstack.util.sanitized_output import sanitized_tail

logger = logging.getLogger("Run Node")


def _clean_error(error: object) -> str:
    return sanitized_tail(str(error), None) or type(
        error
    ).__name__


@contextmanager
def _forward_container_signals(process, enabled):
    """Keep configured Slurm/Docker signals visible to the scientific worker."""
    import signal
    previous = {}
    if enabled and os.name != "nt":
        def forward(number, _frame):
            if process.returncode is None:
                try:
                    process.send_signal(number)
                except ProcessLookupError:
                    pass
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGALRM"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, forward)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


async def _confirm_container_stopped(entry, error, *, in_docker, server=None):
    if getattr(entry, "_science_cleanup_pending", False):
        return  # A launched/possibly reattached execution still needs evidence.
    owner = getattr(entry, "_execution_owner", None)
    local_controller = (not in_docker and getattr(entry, "_execution_local", False)
        and owner is not None and owner["process_id"] == os.getpid())
    managed_native = not in_docker and bool(os.environ.get("SIMSTACK_NATIVE_OBSERVATION_ID"))
    if not local_controller and not managed_native and (not in_docker or os.environ.get("SIMSTACK_SLURM_CONTAINER_EXECUTION") != "1"):
        return
    from simstack.server_api.http_transport import RunnerAPIError
    server = server or context.server
    while True:
        try:
            await server.tasks.termination_confirmed(entry, error)
            return
        except RunnerAPIError as exc:
            if exc.status_code == 409:
                # A superseded identity is already fenced; its science is dead.
                return
            if exc.status_code is not None and exc.status_code < 500:
                raise
            logger.warning("Task %s awaits its physical stop acknowledgement", entry.id, exc_info=True)
            await asyncio.sleep(1)


async def _retain_worker_exit(server, pending, resource):
    """Keep a waited worker's receipt until either durable sink accepts it."""
    from simstack.runner_runtime.pending_execution_failures import record_failure, reconcile_observed_failure
    while True:
        if pending[0] is None:
            try:
                pending = record_failure(server=server, resource=resource, **pending[1]), pending[1]
            except OSError:
                logger.warning("Task %s retains its unpersisted worker exit", pending[1]["execution"]["task_id"], exc_info=True)
        if await reconcile_observed_failure(server, pending):
            return pending
        if pending[0] is not None:
            return pending  # A durable exact-attempt receipt can be replayed later.
        # The controller's exit hides the actual worker code. Keep the only
        # receipt alive when neither the journal nor Server can retain it.
        await asyncio.sleep(1)


async def _run_local_controller(registry_entry, resource_str, project_root, *, in_docker=False):
    """Own the scientific worker until its attempt finishes, even after its caller exits."""
    from simstack.runner_runtime.execution_environment import task_environment, task_launch_command, task_project_root
    from simstack.runner_runtime.launch_claims import inherit_launch_reservation
    from simstack.core.services.task_termination import monitor_task_process

    root = task_project_root(registry_entry, project_root or context.config.project_root)
    command = [*task_launch_command(registry_entry, resource_str, root, local_user=True, in_docker=in_docker), "--local-worker", str(os.getpid())]
    environment = task_environment(registry_entry, server=context.server)
    # The supervisor owns the durable native lease; science shares its session.
    from simstack.runner_runtime.native_observations import adopt
    adopt(context.server, registry_entry)
    environment.pop("SIMSTACK_NATIVE_OBSERVATION_ID", None)
    environment.pop("SIMSTACK_NATIVE_OBSERVATION_EXECUTION", None)
    environment["SIMSTACK_SCIENCE_CONTROLLER_PID"] = str(os.getpid())
    if in_docker and os.name != "nt" and os.getpgrp() != os.getpid():
        os.setsid()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        if in_docker and os.environ.get("SIMSTACK_SLURM_CONTAINER_EXECUTION") == "1":
            # A late-created container cannot execute a superseded Slurm run.
            # This happens inside the live supervisor, never before Docker spawn.
            backend = os.environ.get("SIMSTACK_CONTAINER_BACKEND")
            await context.server.slurm.container_intent(registry_entry,
                **({"backend": backend} if backend else {}))
        with inherit_launch_reservation(None, environment) as ownership:
            object.__setattr__(registry_entry, "_science_cleanup_pending", True)
            try:
                if os.name == "nt":
                    from simstack.runner_runtime.windows_science_job import inherited_job, spawn_job_process
                    job = inherited_job(context.server, registry_entry)
                    process = spawn_job_process(command, name=job.name, job=job,
                        cwd=root, env=environment, stdout=stdout_file, stderr=stderr_file,
                        startupinfo=ownership.get("startupinfo"))
                else:
                    process = await asyncio.create_subprocess_exec(*command, cwd=root, env=environment,
                        stdout=stdout_file, stderr=stderr_file, **ownership)
            except OSError:
                # exec/CreateProcess failed without returning a worker handle.
                object.__setattr__(registry_entry, "_science_cleanup_pending", False)
                raise
        if os.name != "nt" and isinstance(process, asyncio.subprocess.Process) and os.getpgrp() == os.getpid():
            process._simstack_science_group = os.getpid()
        process._simstack_in_container = in_docker
        monitor_error = None
        try:
            with _forward_container_signals(process, in_docker):
                await monitor_task_process(registry_entry, process, server=context.server,
                    runner_pid=os.getpid(), workdir=context.config.workdir)
        except BaseException as exc:
            monitor_error = exc
            raise
        finally:
            from simstack.core.services.task_termination import stop_owned_process_group, TaskTerminationError
            while True:
                try:
                    await stop_owned_process_group(process)
                    if process.returncode is None:
                        await process.wait()
                    object.__setattr__(registry_entry, "_science_cleanup_pending", False)
                    break
                except (OSError, TaskTerminationError):
                    # An inaccessible Job/group cannot become a terminal task
                    # while its descendants may still be executing science.
                    logger.warning("Task %s retains its native execution until cleanup is verified", registry_entry.id, exc_info=True)
                    await asyncio.sleep(1)
            if monitor_error is not None:
                from simstack.runner_runtime.pending_execution_failures import record_observed_failure
                code = process.returncode
                pending = record_observed_failure(_clean_error(monitor_error), phase="subprocess",
                    execution=context.server.tasks.execution(registry_entry), server=context.server,
                    resource=resource_str, exit_code=code if code >= 0 else None,
                    signal=-code if code < 0 else None)
                # Reconciliation uses the original observation/cancellation
                # cause, never a generic exit that could win the failure race.
                await _retain_worker_exit(context.server, pending, resource_str)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = sanitized_tail(stdout_file.read(), None)
        stderr = sanitized_tail(stderr_file.read(), None)
    result = parse_run_node_result(stdout)
    visible = "\n".join(line for line in stdout.splitlines() if not line.startswith(RESULT_PREFIX))
    if visible:
        logger.info("%s", visible)
    if stderr:
        logger.warning("%s", stderr)
    from simstack.runner_runtime.pending_execution_failures import (
        record_failure, record_observed_failure,
    )
    error = (result.error if result is not None else None) or f"Scientific worker exited without an outcome (code {process.returncode})"
    termination = {"exit_code": process.returncode if process.returncode >= 0 else None,
                   "signal": -process.returncode if process.returncode < 0 else None}
    pending = (record_observed_failure((result.error if result is not None else None)
        or f"Scientific worker exited (code {process.returncode})", phase="subprocess",
        execution=context.server.tasks.execution(registry_entry), server=context.server,
        resource=resource_str, return_kind=result.return_kind if result is not None else "exception",
        **termination) if process.returncode else None)

    async def finish(outcome):
        nonlocal pending
        if pending is not None:
            pending = await _retain_worker_exit(context.server, pending, resource_str)
        return outcome

    if result is not None and result.success:
        # A rejected stop ACK must not discard the only in-memory receipt of
        # the worker's physical exit behind this successful protocol record.
        result = await finish(result)
        # Slurm can release/requeue this daemon-owned execution only after
        # its independently supervised science has physically drained.
        await _confirm_container_stopped(registry_entry, "Scientific execution stopped", in_docker=in_docker)
        # A successful protocol record follows Server's committed completion;
        # no additional read can invalidate it during an API outage.
        return result
    from simstack.server_api.http_transport import RunnerAPIError
    journaled = pending is not None and pending[0] is not None
    while True:
        try:
            recorded = await context.server.tasks.get_outcome(registry_entry)
            if recorded is not None and recorded.status == TaskStatus.COMPLETED:
                completed = await finish(RunNodeResult(True, recorded.return_kind or "none"))
                await _confirm_container_stopped(registry_entry, "Scientific execution stopped", in_docker=in_docker)
                return completed
            if recorded is not None and recorded.status == TaskStatus.TERMINATING:
                # The waited handle proves science has stopped even when it
                # exited before the monitor's first cancellation poll.
                recorded = await context.server.tasks.termination_confirmed(registry_entry, recorded.error or TERMINATED_BY_USER)
            elif recorded is None or recorded.status in {TaskStatus.SUBMITTED, TaskStatus.RETRIEVED, TaskStatus.RUNNING,
                                                       TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING}:
                recorded = await context.server.tasks.fail_attempt(registry_entry, error, phase="subprocess",
                    return_kind=result.return_kind if result is not None else "exception", **termination)
            if recorded is not None:
                error = recorded.error or error
                await _confirm_container_stopped(registry_entry, error, in_docker=in_docker)
            kind = getattr(recorded, "return_kind", None) or (result.return_kind if result is not None else "exception")
            return await finish(RunNodeResult(False, kind, error))
        except RunnerAPIError as exc:
            if not journaled or pending is not None and exc.status_code in {403, 409}:
                try:
                    if pending is not None:
                        path = record_failure(server=context.server, resource=resource_str,
                            rejected_status=exc.status_code, **pending[1])
                        pending = path, pending[1]
                    else:
                        path = record_failure(error, phase="subprocess", execution=context.server.tasks.execution(registry_entry),
                            server=context.server, resource=resource_str,
                            return_kind=result.return_kind if result is not None else "exception",
                            rejected_status=exc.status_code, **termination)
                    journaled = path is not None
                except OSError:
                    logger.warning("Task %s could not journal its worker outcome", registry_entry.id, exc_info=True)
            if exc.status_code is not None and exc.status_code < 500:
                return await finish(RunNodeResult(False, result.return_kind if result is not None else "exception", error))
            logger.warning("Task %s awaits its stopped worker outcome", registry_entry.id, exc_info=True)
            await asyncio.sleep(1)


def _guard_controller_lifetime(controller_pid):
    """A worker stops its own session if its supervisor disappears."""
    import signal
    import threading
    import time

    group = os.getpgrp()
    if group != controller_pid:
        raise RuntimeError("Scientific worker does not belong to its controller session")
    def watch():
        while os.getppid() == controller_pid:
            time.sleep(.1)
        # This process is still a member of this exact group, so its group ID
        # cannot have been recycled for an unrelated execution.
        os.killpg(os.getpgrp(), signal.SIGKILL)
    threading.Thread(target=watch, name="science-controller-lifetime", daemon=True).start()


async def run_node_from_id(
    node_id: str,
    resource_str: str,
    project_root: str | None = None,
    in_docker: bool = False,
    *, local_worker: int | None = None,
) -> RunNodeResult:
    from types import SimpleNamespace
    from simstack.runner_runtime.launch_claims import task_execution
    if os.name == "nt":
        from simstack.runner_runtime.windows_science_job import retain_inherited_job
        retain_inherited_job()
    if local_worker is not None and (local_worker < 1 or os.getppid() != local_worker):
        return RunNodeResult(False, "none", "The local worker no longer belongs to its controller")
    if (local_worker is not None and os.name != "nt"
            and os.environ.get("SIMSTACK_SCIENCE_CONTROLLER_PID") == str(local_worker)):
        _guard_controller_lifetime(local_worker)
    with task_execution(SimpleNamespace(id=node_id)):
        return await _run_node_from_id(node_id, resource_str, project_root, in_docker, local_worker=local_worker)


async def _run_node_from_id(
    node_id: str,
    resource_str: str,
    project_root: str | None = None,
    in_docker: bool = False,
    *, local_worker: int | None = None,
) -> RunNodeResult:
    """Execute the task only under the identity passed by its launcher."""
    from simstack.runner_runtime.launch_claims import current_execution
    execution = dict(current_execution() or {})
    host_controller = bool(os.environ.get("SIMSTACK_LOCAL_EXECUTION_TOKEN") and not in_docker and local_worker is None)
    logger.info(f"Initializing node run: node_id={node_id}, resource={resource_str}, project_root={project_root}, in_docker={in_docker}")
    from simstack.server_api.http_transport import RunnerAPIError
    while True:
        try:
            init_kwargs = {
            "resource": resource_str,
            "project_root": project_root,
            "in_docker": bool(in_docker),
            }
            if host_controller:
                init_kwargs["load_catalogue"] = False
            # Host resource workdir is bind-mounted at CONTAINER_WORKDIR.
            if in_docker:
                init_kwargs["workdir"] = CONTAINER_WORKDIR
            await context.initialize(**init_kwargs)
            break
        except (Exception, SystemExit) as e:
            if host_controller and isinstance(e, RunnerAPIError) and (e.status_code is None or e.status_code >= 500):
                logger.warning("Local execution owner awaits API bootstrap", exc_info=True)
                await asyncio.sleep(1)
                continue
            error = _clean_error(e)
            logger.error("Failed to initialize context: %s", error)
            from simstack.runner_runtime.pending_execution_failures import record_failure
            record_failure(error, phase="bootstrap", exit_code=1, resource=resource_str)
            return RunNodeResult(False, "none", error)
    registry_entry = None
    scheduler_batch = (os.environ.get("SIMSTACK_SLURM_SUBMISSION_TASK") == str(node_id)
        and not in_docker)
    if scheduler_batch:
        # A restarted batch script carries the original immutable launch, not
        # the admitted execution of its previous process. Admission happens
        # before any failure can be attributed to a scientific attempt.
        try:
            if os.environ.get("SLURM_ARRAY_TASK_ID") is not None:
                return RunNodeResult(False, "none", "Slurm arrays require explicit child tasks or MassRunner")
            admitted = await context.server.slurm.start_incarnation(execution,
                os.environ["SIMSTACK_SLURM_SUBMISSION"], os.environ["SLURM_JOB_ID"],
                int(os.environ.get("SLURM_RESTART_COUNT", "0")), os.getpid())
            registry_entry = admitted["task"]
            if admitted["disposition"] == "completed":
                return RunNodeResult(True, registry_entry.return_kind or "none")
            execution = context.server.tasks.execution(registry_entry)
            from simstack.runner_runtime.launch_claims import adopt_execution
            adopt_execution(execution)
            os.environ["SIMSTACK_EXECUTION_START"] = execution["start_id"]
        except (Exception, SystemExit) as exc:
            error = _clean_error(exc)
            logger.warning("Scheduler batch was not admitted: %s", error)
            return RunNodeResult(False, "none", error)
    try:
        if (execution.get("task_id") != str(node_id)
                or not execution.get("claim_id") or not execution.get("start_id")):
            return RunNodeResult(False, "none", "A launcher execution identity is required")
        while True:
            try:
                observed = await context.server.tasks.get_task(node_id)
                break
            except RunnerAPIError as exc:
                if not host_controller or exc.status_code is not None and exc.status_code < 500:
                    raise
                logger.warning("Local execution owner awaits its admitted task", exc_info=True)
                await asyncio.sleep(1)
        if observed is not None:
            matches = str(observed.id) == execution["task_id"] and all(
                execution.get(key) == getattr(observed, "_execution_" + key, None)
                for key in ("generation", "claim_id")
            )
            canonical_start = getattr(observed, "_execution_start_id", None)
            if not matches or canonical_start not in {None, execution["start_id"]}:
                return RunNodeResult(False, "none", "The task no longer belongs to this launch")
            object.__setattr__(observed, "_execution_start_id", execution["start_id"])
        registry_entry = observed
        if not registry_entry:
            logger.error("Server has no task with ID %s", node_id)
            return RunNodeResult(False, "none", f"Node {node_id} not found")
        if getattr(registry_entry, "_execution_local", False) and not in_docker and local_worker is None:
            if registry_entry.status == TaskStatus.TERMINATING and not getattr(registry_entry, "_execution_owner", None):
                await context.server.tasks.termination_confirmed(registry_entry, registry_entry.error or TERMINATED_BY_USER)
                return RunNodeResult(False, "none", registry_entry.error or TERMINATED_BY_USER)
            if registry_entry.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIME_OUT}:
                return RunNodeResult(registry_entry.status == TaskStatus.COMPLETED, registry_entry.return_kind or "none", registry_entry.error)
            from simstack.runner_runtime.launch_claims import record_spawn
            record_spawn(context.server, registry_entry, os.getpid())
            while True:
                try:
                    registry_entry = await context.server.tasks.record_process(registry_entry, os.getpid(), host_owner=True)
                    break
                except RunnerAPIError as exc:
                    if exc.status_code is not None and exc.status_code < 500:
                        raise
                    logger.warning("Local execution owner awaits its physical admission", exc_info=True)
                    await asyncio.sleep(1)
            object.__setattr__(registry_entry, "_execution_start_id", execution["start_id"])
        if not local_worker and (
                in_docker or (not registry_entry.parameters.in_docker and (
                    getattr(registry_entry, "_execution_local", False)
                    or os.environ.get("SIMSTACK_NATIVE_OBSERVATION_ID")))):
            return await _run_local_controller(registry_entry, resource_str, project_root, in_docker=in_docker)
        from simstack.runner_runtime.execution_environment import prepare_task
        repository_root = await prepare_task(registry_entry)
        if repository_root is not None:
            from simstack.runner_runtime.execution_environment import task_resource_config
            machine_config = task_resource_config(registry_entry, context)
            context.config._project_root = repository_root
            context._resource_config = machine_config
            os.environ["SIMSTACK_REPOSITORY_REVISION"] = str(registry_entry.repository_revision)
            if not host_controller:
                await context.refresh_mappings()
        if registry_entry.status == TaskStatus.TERMINATING:
            if host_controller and getattr(registry_entry, "_execution_owner", None):
                # No scientific worker/container was launched by this owner.
                await context.server.tasks.termination_confirmed(registry_entry, registry_entry.error or TERMINATED_BY_USER)
            return RunNodeResult(
                False,
                "none",
                registry_entry.error or TERMINATED_BY_USER,
            )
        parameters = registry_entry.parameters
        if parameters.in_docker and not in_docker:
            docker_result = await run_docker_with_outcome(registry_entry)
            if not docker_result.success:
                return RunNodeResult(
                    False, docker_result.return_kind, docker_result.error
                )
            # The container runner already verified its committed outcome.
            return RunNodeResult(True, docker_result.return_kind)
        outcome = await run_node_from_registry_with_outcome(registry_entry,
            process_id=local_worker)
        return RunNodeResult(outcome.success, outcome.return_kind)
    except (Exception, SystemExit) as e:
        error = _clean_error(e)
        logger.error("Error running node task_id: %s: %s", node_id, error)
        if registry_entry:
            from simstack.runner_runtime.pending_execution_failures import record_observed_failure
            pending = record_observed_failure(error, phase="execute", execution=context.server.tasks.execution(registry_entry), exit_code=1,
                server=context.server, resource=resource_str)
            if pending[0] is None:
                # A secondary journal outage cannot skip the stopped owner's
                # ACK or discard this original failure while Server is offline.
                await _retain_worker_exit(context.server, pending, resource_str)
            while True:
                try:
                    await context.server.tasks.fail_attempt(registry_entry, error)
                    break
                except RunnerAPIError as exc:
                    if not host_controller or not getattr(registry_entry, "_execution_owner", None) or exc.status_code is not None and exc.status_code < 500:
                        logger.warning("Failure outcome retained for reconciliation", exc_info=True)
                        break
                    logger.warning("Local execution owner awaits its stopped-worker outcome", exc_info=True)
                    await asyncio.sleep(1)
                except Exception:
                    logger.warning("Failure outcome retained for reconciliation", exc_info=True)
                    break
            if host_controller:
                await _confirm_container_stopped(registry_entry, error, in_docker=False)
        return RunNodeResult(False, "exception", error)


def run_node_main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', stream=sys.stdout)
    logger.info("Starting run_node_main")
    parser = argparse.ArgumentParser(description="Run nodes for a specific resource")
    parser.add_argument(
        "--node-id",
        type=str,
        help="Specific node ID to run (overrides resource-based polling)",
    )

    parser.add_argument(
        "--resource",
        default="local",
        nargs="?",
        type=str,
        help="resource to load",
    )

    parser.add_argument(
        "--project-root",
        default=None,
        nargs="?",
        type=str,
        help="project root directory",
    )

    parser.add_argument(
        "--in-docker",
        action="store_true",
        help="run inside a docker container",
    )
    parser.add_argument("--local-worker", type=int, help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.node_id:
        result = asyncio.run(
            run_node_from_id(
                args.node_id,
                args.resource,
                args.project_root,
                args.in_docker,
                local_worker=args.local_worker,
            )
        )
        print(encode_run_node_result(result))
        if not result.success:
            raise SystemExit(1)


if __name__ == "__main__":
    run_node_main()
