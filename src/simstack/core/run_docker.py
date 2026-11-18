import asyncio
import logging
import os
import platform
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.run_node_protocol import RESULT_PREFIX, parse_run_node_result
from simstack.models import NodeRegistry
from simstack.runner_runtime.apptainer import build_apptainer_command
from simstack.runner_runtime.container_environment import (
    CONTAINER_WORKDIR as CONTAINER_WORKDIR,
    LEGACY_CONTAINER_WORKDIR as LEGACY_CONTAINER_WORKDIR,
    ensure_host_task_workdir,
    host_project_file_mounts,
    prepare_container_environment,
)
from simstack.runner_runtime.container_resources import (
    container_resource_args,
    docker_cpu_limit,
    docker_memory_limit,
    format_container_failure_error,
)
from simstack.runner_runtime.docker_engine import (
    _harmonize_docker_task_dir,
    build_docker_command,
    docker_cidfile_path,
    docker_daemon_identity,
    docker_execution_name,
    inspect_docker_execution,
    inspect_docker_oomkilled,
    launcher_owner_ids,
    prepare_docker_cidfile,
    pull_docker_image,
    read_container_id,
    resolve_docker_pull_ref as resolve_docker_pull_ref,
)
from simstack.util.sanitized_output import (
    sanitized_command,
    sanitized_tail,
)

logger = logging.getLogger("DockerRunner")


@dataclass(frozen=True)
class DockerRunResult:
    success: bool
    return_kind: str
    error: str | None = None


async def _persist_post_launch_failure(
    launch_entry: NodeRegistry, error: str, *, diagnostics=None, returncode: int | None = None,
    return_kind="exception", pending_failure=None,
) -> None:
    """Report the original launch; a child error or competing start wins on Server."""
    from simstack.runner_runtime.pending_execution_failures import record_failure
    termination = {
        "exit_code": returncode if returncode is not None and returncode >= 0 else None,
        "signal": -returncode if returncode is not None and returncode < 0 else None,
    }
    if pending_failure is None:
        record_failure(error, phase="container", execution=context.server.tasks.execution(launch_entry),
            server=context.server, resource=getattr(launch_entry, "_execution_resource", None), return_kind=return_kind, **termination)
    else:
        # Retain the already-known scientific cause before the failure POST or
        # receipt delivery can become the first authoritative Server outcome.
        pending_failure[1].update(error=sanitized_tail(error, None), return_kind=return_kind)
        try:
            record_failure(server=context.server,
                resource=getattr(launch_entry, "_execution_resource", None), **pending_failure[1])
        except OSError:
            logger.warning("Could not update task %s container failure cause", launch_entry.id, exc_info=True)
    from simstack.server_api.http_transport import RunnerAPIError
    while True:
        try:
            await context.server.tasks.fail_attempt(launch_entry, error, phase="container", diagnostics=diagnostics,
                return_kind=return_kind, **termination)
            break
        except RunnerAPIError as exc:
            if not getattr(launch_entry, "_execution_owner", None) or exc.status_code is not None and exc.status_code < 500:
                logger.warning("Container failure retained for task %s reconciliation", launch_entry.id, exc_info=True)
                break
            logger.warning("Local execution owner awaits its stopped-container outcome", exc_info=True)
            await asyncio.sleep(1)
        except Exception:
            logger.warning("Container failure retained for task %s reconciliation", launch_entry.id, exc_info=True)
            break


def _container_oom_killed(
    docker_cmd: str, cidfile: Path | None
) -> bool | None:
    if docker_cmd != "docker" or cidfile is None:
        return None
    container_id = read_container_id(cidfile)
    if not container_id:
        return None
    return inspect_docker_oomkilled(container_id)


def _reload_resource_config() -> None:
    """Re-read config.toml so docker_image assignments from git pull are visible."""
    resource_config = context.resource_config
    if resource_config is None:
        return
    resource_config.reload()
    logger.info("Reloaded resource config from %s", resource_config._config_path)


def _docker_program_config(registry_entry: NodeRegistry, resource_config=None) -> tuple[dict, str]:
    """Look up ``[resource.program.<node>]`` after config.toml has been reloaded.

    ``self`` means the resource on which this runner is currently executing.
    """
    resource_config = resource_config if resource_config is not None else context.resource_config
    task_resource = str(registry_entry.parameters.resource)
    lookup_resource = (
        str(context.config.resource) if task_resource == "self" else task_resource
    )
    try:
        program_config = resource_config.get_program(
            registry_entry.name, resource=lookup_resource
        )
    except ValueError:
        program_config = {}
    return program_config, lookup_resource


async def _register_local_backend(launch_entry: NodeRegistry, process_id: int, backend: str) -> None:
    # Publish selection before science can start. The host controller,
    # rather than the container CLI, remains the admitted physical owner.
    from simstack.server_api.http_transport import RunnerAPIError
    while True:
        try:
            await context.server.tasks.record_process(launch_entry,
                process_id, host_owner=True, container_backend=backend)
            break
        except RunnerAPIError as exc:
            if exc.status_code is not None and exc.status_code < 500:
                raise
            logger.warning("Local execution owner awaits container backend registration", exc_info=True)
            await asyncio.sleep(1)


async def _register_docker_launch(launch_entry, execution, *, managed: bool):
    """Register the physical Docker owner before the CLI can start the task."""
    container_daemon = None
    container_name = docker_execution_name(context.server, execution)
    from simstack.runner_runtime.launch_claims import record_container_launch
    if managed:
        container_daemon = await asyncio.to_thread(docker_daemon_identity)
    replay_container = record_container_launch(context.server, launch_entry, container_name,
        **({"daemon_id": container_daemon} if managed else {}))
    if managed:
        from simstack.server_api.http_transport import RunnerAPIError
        while True:
            try:
                await context.server.tasks.admit_container(launch_entry)
                break
            except RunnerAPIError as exc:
                if exc.status_code is not None and exc.status_code < 500:
                    raise
                await asyncio.sleep(1)
    if replay_container:
        # Its previous CLI may have committed a daemon-owned container.
        object.__setattr__(launch_entry, "_science_cleanup_pending", True)
    return container_name, container_daemon, replay_container


async def _acknowledge_docker_stop(launch_entry, execution, outcome, observed, *, managed: bool):
    if observed is None or observed["State"]["Status"] in {"exited", "dead"}:
        from simstack.server_api.http_transport import RunnerAPIError
        while True:
            try:
                await context.server.tasks.termination_confirmed(launch_entry,
                    outcome.error or "Scientific execution stopped")
                if managed:
                    from simstack.runner_runtime.launch_claims import finish, receipt
                    value = receipt(context.server, launch_entry.id)
                    if value is not None and value.get("claim_id") == execution["claim_id"]:
                        finish(context.server, value, claimed=False)
                break
            except RunnerAPIError as exc:
                if managed and exc.status_code == 409:
                    # A durable failure may not have reached Server yet.
                    # The retained daemon receipt retries after failure
                    # reconciliation, without falsely acknowledging stop.
                    break
                if exc.status_code is not None and exc.status_code < 500:
                    raise
                await asyncio.sleep(1)


async def _release_local_container(launch_entry, outcome, *, process, workdir):
    if process is not None:
        from simstack.core.services.task_termination import (
            TaskTerminationError,
            stop_task_execution,
        )
        from simstack.server_api.http_transport import RunnerAPIError
        while True:
            try:
                await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                    workdir=workdir, local_process=process)
                object.__setattr__(launch_entry, "_science_cleanup_pending", False)
                break
            except (RunnerAPIError, TaskTerminationError, OSError):
                logger.warning("Local container owner awaits verified physical cleanup", exc_info=True)
                await asyncio.sleep(1)
    # Only the host owner can acknowledge its entire drained execution.
    from simstack.core.run_node import _confirm_container_stopped
    await _confirm_container_stopped(launch_entry,
        outcome.error or "Scientific execution stopped", in_docker=False, server=context.server)


async def run_docker_with_outcome(registry_entry: NodeRegistry, *, on_observed_process=None,
                                retry_unpersisted_evidence=False) -> DockerRunResult:
    from simstack.runner_runtime.execution_environment import task_project_root, task_resource_config
    from simstack.runner_runtime.launch_claims import current_execution, task_execution
    launch_entry = registry_entry
    local_owner = getattr(launch_entry, "_execution_owner", None)
    if getattr(launch_entry, "_execution_local", False) and (
            not local_owner or local_owner["process_id"] != os.getpid()):
        raise ValueError("A local container must be launched by its admitted physical owner")
    with task_execution(launch_entry):
        execution = current_execution()
        if execution is None:
            raise RuntimeError("A container launch requires its claim identity")
        object.__setattr__(launch_entry, "_execution_start_id", execution["start_id"])
    _reload_resource_config()
    resource_config = task_resource_config(registry_entry, context)
    program_config, lookup_resource = _docker_program_config(registry_entry, resource_config)

    image = program_config.get("docker_image", None)
    if image is None:
        logger.error(f"Docker image for {registry_entry.name} not found for task_id: {registry_entry.id} (looked up resource={lookup_resource})")
        error = f"Docker image for {registry_entry.name} not found (resource={lookup_resource})"
        await _persist_post_launch_failure(launch_entry, error)
        if local_owner:
            from simstack.core.run_node import _confirm_container_stopped
            await _confirm_container_stopped(launch_entry, error, in_docker=False, server=context.server)
        return DockerRunResult(False, "none", error)
    admitted_container = getattr(launch_entry, "_execution_container_owner", None) == execution
    docker_cmd = "docker" if admitted_container else program_config.get("docker_cmd", None)
    if docker_cmd is None:
        docker_cmd = "docker"
    if docker_cmd in {"docker", "apptainer"}:
        object.__setattr__(launch_entry, "_execution_container_backend", docker_cmd)
        if local_owner:
            await _register_local_backend(launch_entry, local_owner["process_id"], docker_cmd)

    if docker_cmd == "apptainer":
        if not image.endswith(".sif") and not image.startswith("docker://"):
            image += ".sif"

    workdir = context.config.workdir
    task_dir = ensure_host_task_workdir(workdir, registry_entry.name, str(registry_entry.id))
    cidfile: Path | None = None
    if docker_cmd == "docker":
        cidfile = docker_cidfile_path(task_dir)
    slurm_container = not admitted_container and registry_entry.parameters.queue == "slurm-queue"
    managed_container = docker_cmd == "docker" and not local_owner and not slurm_container
    project_root = task_project_root(registry_entry, context.config.project_root)
    environment = await prepare_container_environment(
        registry_entry, context, docker_cmd, project_file_mounts=host_project_file_mounts(project_root),
        slurm_container=slurm_container,
    )
    container_environment = environment.variables
    slurm_parameters = getattr(registry_entry.parameters, "slurm_parameters", None)
    in_allocation = slurm_container or bool(os.environ.get("SLURM_JOB_ID"))
    resource_args = container_resource_args(docker_cmd, slurm_parameters, in_allocation=in_allocation)
    if resource_args:
        logger.info(
            "task_id: %s applying container resource limits from slurm_parameters: %s",
            registry_entry.id,
            shlex.join(resource_args),
        )
    elif docker_cmd == "apptainer" and (
        docker_cpu_limit(slurm_parameters) is not None
        or docker_memory_limit(slurm_parameters) is not None
    ):
        logger.info(
            "task_id: %s omitting apptainer --cpus/--memory (need cgroups v2); "
            "Slurm already limits the job",
            registry_entry.id,
        )

    # Prefer the task resource inside the container (self -> local for image/workdir lookups).
    task_args = [
        "--node-id", str(registry_entry.id),
        "--resource", lookup_resource,
        "--project-root", environment.project_root,
        "--in-docker",
    ]
    container_name = None
    container_daemon = None
    replay_container = False
    if docker_cmd == "docker":
        docker_registry = None
        if resource_config is not None:
            docker_registry = resource_config.get_docker_registry(lookup_resource)
        await pull_docker_image(image, docker_registry)
        if cidfile is None:
            raise RuntimeError("docker cidfile was not prepared")
        container_name, container_daemon, replay_container = await _register_docker_launch(
            launch_entry, execution, managed=managed_container,
        )
        cmd = build_docker_command(
            environment, image=image, workdir=workdir, resource_args=resource_args,
            task_args=task_args, container_name=container_name, cidfile=cidfile,
        )
    elif docker_cmd == "apptainer":
        cmd = build_apptainer_command(
            environment, image=image, workdir=workdir, resource_args=resource_args, task_args=task_args,
        )
    else:
        error = f"Unsupported command {docker_cmd}"
        await _persist_post_launch_failure(launch_entry, error)
        if local_owner:
            from simstack.core.run_node import _confirm_container_stopped
            await _confirm_container_stopped(launch_entry, error, in_docker=False, server=context.server)
        return DockerRunResult(False, "none", error)

    sanitized_cmd = sanitized_command(cmd, None)

    logger.info(
        "starting docker for task_id: %s full docker command: %s",
        registry_entry.id,
        shlex.join(sanitized_cmd),
    )

    # DETACHED_PROCESS opens a console on Windows. CREATE_NO_WINDOW hides it.
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000
        )

    process_started = False
    canonical = None
    child_outcome = None
    exit_code = None
    failure_cause = None
    pending_failure = None
    observation = None
    observation_lease = None

    async def observe_container():
        while True:
            try:
                if managed_container and await asyncio.to_thread(docker_daemon_identity) != container_daemon:
                    raise RuntimeError("Docker execution belongs to a different daemon")
                return await asyncio.to_thread(inspect_docker_execution, container_name)
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                logger.warning("Task %s awaits Docker execution evidence", launch_entry.id, exc_info=True)
                await asyncio.sleep(1)

    try:
        from simstack.runner_runtime.launch_claims import inherit_launch_reservation
        if (docker_cmd == "apptainer" and not local_owner
                and registry_entry.parameters.queue == "default" and os.name != "nt"):
            from simstack.runner_runtime import native_observations
            observation, observation_lease = native_observations.prepare(context.server, launch_entry)
        while True:
            actual_command = cmd
            if docker_cmd == "docker":
                observed = await observe_container() if replay_container else None
                if observed is None:
                    prepare_docker_cidfile(task_dir)
                else:
                    action = ["start", "--attach"] if observed["State"]["Status"] == "created" else ["logs", "--follow"]
                    actual_command = ["docker", *action, container_name]
            with inherit_launch_reservation(getattr(launch_entry, "_launch_reservation", None), container_environment) as ownership:
                try:
                    object.__setattr__(launch_entry, "_science_cleanup_pending", True)
                    from contextlib import nullcontext
                    observation_owner = (native_observations.inherit(observation_lease, ownership)
                                         if observation is not None else nullcontext(ownership))
                    with observation_owner as ownership:
                        if observation is not None:
                            actual_command = native_observations.observed_command(context.server, observation, actual_command)
                        process = await asyncio.create_subprocess_exec(
                            *actual_command,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            creationflags=creationflags,
                            start_new_session=platform.system() != "Windows",
                            env=container_environment,
                            **ownership,
                        )
                    process_started = True
                    if observation is not None:
                        process._simstack_process_group = True
                        native_observations.spawned(context.server, observation, process.pid)
                        if on_observed_process is not None:
                            on_observed_process(process, observation)
                except OSError:
                    if docker_cmd != "docker" or not replay_container:
                        object.__setattr__(launch_entry, "_science_cleanup_pending", False)
                        raise
                    logger.warning("Task %s awaits attachment to its Docker execution", launch_entry.id, exc_info=True)
                    await asyncio.sleep(1)
                    continue
                finally:
                    if observation_lease is not None:
                        observation_lease.close()
                        observation_lease = None
            process_started = True
            if docker_cmd == "apptainer" and os.name != "nt" and isinstance(process, asyncio.subprocess.Process):
                process._simstack_process_group = True
            from simstack.runner_runtime.launch_claims import record_spawn
            record_spawn(context.server, launch_entry, os.getpid() if local_owner else process.pid)
            from simstack.core.services.task_termination import record_process_id
            try:
                if not local_owner:
                    await record_process_id(launch_entry, process.pid, context.server)
            except Exception:
                logger.warning("Task %s process report awaits reconciliation", launch_entry.id, exc_info=True)
            if local_owner:
                from simstack.core.services.task_termination import monitor_task_process
                stdout_b, stderr_b = await monitor_task_process(launch_entry, process, server=context.server,
                    workdir=workdir, runner_pid=os.getpid())
            else:
                stdout_b, stderr_b = await process.communicate()
            exit_code = process.returncode
            child_outcome = parse_run_node_result(sanitized_tail(stdout_b, None))
            if docker_cmd == "docker" and (replay_container or exit_code != 0):
                observed = await observe_container()
                if observed is not None:
                    if observed["State"]["Status"] not in {"exited", "dead"}:
                        replay_container = True
                        continue
                    exit_code = observed["State"]["ExitCode"]
                elif exit_code < 0:
                    # A killed CLI cannot tell whether its create committed.
                    # Repeating the same Docker name cannot create a second job.
                    replay_container = True
                    continue
            if exit_code != 0:
                failure_cause = format_container_failure_error(exit_code, docker_cmd=docker_cmd)
                from simstack.runner_runtime.pending_execution_failures import (
                    record_observed_failure,
                )
                pending_failure = record_observed_failure(
                    (child_outcome.error if child_outcome is not None else None) or failure_cause, phase="container",
                    execution=context.server.tasks.execution(launch_entry), server=context.server,
                    resource=getattr(launch_entry, "_execution_resource", None),
                    exit_code=exit_code if exit_code >= 0 else None,
                    signal=-exit_code if exit_code < 0 else None,
                    return_kind=child_outcome.return_kind if child_outcome is not None else "exception",
                    # The normal Runner poll can replay this receipt while the
                    # observer still awaits its owned Apptainer descendants.
                    cleanup={"scope": observation["scope"], "process_group": process.pid}
                        if observation is not None else None)
            if observation is not None:
                from simstack.core.services.task_termination import (
                    TaskTerminationError,
                    stop_owned_process_group,
                )
                while True:
                    try:
                        await stop_owned_process_group(process)
                        break
                    except TaskTerminationError:
                        logger.warning("Task %s awaits its owned Apptainer process group", launch_entry.id, exc_info=True)
                        await asyncio.sleep(1)
            canonical = await context.server.tasks.get_outcome(launch_entry)
            if local_owner and canonical is not None and canonical.status == TaskStatus.TERMINATING:
                from simstack.core.services.task_termination import (
                    TaskTerminationError,
                    stop_task_execution,
                )
                from simstack.server_api.http_transport import RunnerAPIError
                while True:
                    try:
                        await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                            workdir=workdir, local_process=process)
                        canonical = await context.server.tasks.termination_confirmed(launch_entry, canonical.error or "Terminated by user")
                        break
                    except (RunnerAPIError, TaskTerminationError):
                        logger.warning("Task %s retains its container until stop is acknowledged", launch_entry.id, exc_info=True)
                        await asyncio.sleep(1)
            break
        stdout = sanitized_tail(stdout_b, None)
        stderr = sanitized_tail(stderr_b, None)
        if canonical is None:
            raise RuntimeError("Container task outcome is unavailable")
        if canonical.status == TaskStatus.COMPLETED:
            # A launcher/cleanup failure cannot revoke a committed result.
            outcome = DockerRunResult(True, canonical.return_kind or (
                child_outcome.return_kind if child_outcome else "none"))
        elif exit_code != 0 or child_outcome is None or not child_outcome.success:
            memory = docker_memory_limit(slurm_parameters, uppercase=docker_cmd == "apptainer")
            allocated = docker_cmd == "apptainer" and in_allocation
            base_error = format_container_failure_error(exit_code,
                oom_killed=_container_oom_killed(docker_cmd, cidfile),
                memory_limit=None if allocated else memory, allocation_memory=memory if allocated else None,
                docker_cmd=docker_cmd)
            error = canonical.error or (child_outcome.error if child_outcome else None) or base_error
            error = sanitized_tail(error, None, limit=None)
            visible_stdout = "\n".join(line for line in stdout.splitlines() if not line.startswith(RESULT_PREFIX))
            diagnostics = [base_error, *(part for part in (visible_stdout, stderr) if part)]
            logger.error("Container task_id: %s failed: %s", launch_entry.id, error)
            await _persist_post_launch_failure(launch_entry, error, diagnostics=diagnostics, returncode=exit_code,
                return_kind=canonical.return_kind or (child_outcome.return_kind if child_outcome else "exception"),
                pending_failure=pending_failure)
            outcome = DockerRunResult(False, child_outcome.return_kind if child_outcome else "exception", error)
        else:
            error = canonical.error or "Container exited without completing its task"
            await _persist_post_launch_failure(launch_entry, error, returncode=exit_code, return_kind=child_outcome.return_kind)
            outcome = DockerRunResult(False, child_outcome.return_kind, error)
    except Exception as exc:
        if process_started and (local_owner or docker_cmd == "docker" or process.returncode is None):
            from simstack.core.services.task_termination import (
                TaskTerminationError,
                stop_task_execution,
            )
            from simstack.server_api.http_transport import RunnerAPIError
            while True:
                try:
                    await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                        workdir=workdir, local_process=process)
                    await process.wait()
                    break
                except (RunnerAPIError, TaskTerminationError):
                    if not local_owner and docker_cmd != "docker":
                        raise
                    logger.warning("Task %s retains its container until cleanup is verified", launch_entry.id, exc_info=True)
                    await asyncio.sleep(1)
            if local_owner and not (child_outcome is not None and child_outcome.success):
                while True:
                    try:
                        canonical = await context.server.tasks.get_outcome(launch_entry)
                        if canonical is not None and canonical.status == TaskStatus.TERMINATING:
                            canonical = await context.server.tasks.termination_confirmed(
                                launch_entry, canonical.error or "Terminated by user")
                        break
                    except RunnerAPIError as failure:
                        if failure.status_code is not None and failure.status_code < 500:
                            break
                        logger.warning("Task %s awaits its stopped-container outcome", launch_entry.id, exc_info=True)
                        await asyncio.sleep(1)
        error = sanitized_tail(str(exc), None)
        logger.error("Container launch for task %s failed: %s", launch_entry.id, error)
        if canonical is not None and canonical.status == TaskStatus.COMPLETED:
            outcome = DockerRunResult(True, canonical.return_kind or "none")
        elif child_outcome is not None and child_outcome.success:
            # The child only reports success after Server accepts completion.
            # A subsequent read outage is supplemental launcher diagnostics.
            outcome = DockerRunResult(True, child_outcome.return_kind)
        else:
            primary = (canonical.error if canonical is not None and canonical.error else
                (child_outcome.error if child_outcome is not None else None) or failure_cause or error)
            await _persist_post_launch_failure(launch_entry, primary, returncode=exit_code,
                return_kind=(canonical.return_kind if canonical is not None else None)
                    or (child_outcome.return_kind if child_outcome else "exception"),
                pending_failure=pending_failure)
            outcome = DockerRunResult(False, "exception", primary)
    finally:
        if observation_lease is not None:
            observation_lease.close()

    if pending_failure is not None:
        from simstack.runner_runtime.pending_execution_failures import (
            reconcile_observed_failure,
        )
        while not await reconcile_observed_failure(context.server, pending_failure):
            if pending_failure[0] is not None or not retry_unpersisted_evidence:
                break
            await asyncio.sleep(1)
    if observation is not None:
        # Recovery validates the inherited lease and the whole host process
        # group; a CLI exit alone never proves all scientific descendants dead.
        await native_observations.recover(context.server)
    if (slurm_container or managed_container) and docker_cmd == "docker":
        # The compute-side Docker owner can also acknowledge bootstrap/OOM
        # failures where an inside-container supervisor never got to start.
        observed = await observe_container() if process_started else None
        await _acknowledge_docker_stop(launch_entry, execution, outcome, observed, managed=managed_container)
    if local_owner:
        await _release_local_container(
            launch_entry, outcome, process=process if process_started else None, workdir=workdir,
        )
    if process_started and docker_cmd == "docker" and platform.system() != "Windows":
        try:
            owner_uid, owner_gid = launcher_owner_ids()
            await _harmonize_docker_task_dir(image, task_dir, owner_uid, owner_gid)
        except Exception as exc:
            logger.error("Task %s output ownership cleanup failed: %s", launch_entry.id,
                sanitized_tail(str(exc), None))
    return outcome


async def run_docker(registry_entry: NodeRegistry, *, on_observed_process=None, retry_unpersisted_evidence=False) -> bool:
    """Run a container while preserving the existing boolean API."""
    return (await run_docker_with_outcome(registry_entry, on_observed_process=on_observed_process,
        retry_unpersisted_evidence=retry_unpersisted_evidence)).success
