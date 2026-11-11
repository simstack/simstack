import asyncio
import json
import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Any

from simstack.models.document import Model

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_task
from simstack.core.run_docker import run_docker
from simstack.core.services.base_service import BaseService
from simstack.core.services.task_termination import record_process_id, terminate_task
from simstack.core.simstack_result import SimstackResult
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.util.sanitized_output import sanitized_command, sanitized_tail

logger = logging.getLogger("NodeRunner")


async def _spawn_native_process(*command, **options):
    """Keep the dispatch boundary shared while Windows adds atomic Job ownership."""
    if os.name != "nt":
        return await asyncio.create_subprocess_exec(*command, **options)
    from simstack.runner_runtime.windows_science_job import spawn_job_process
    return spawn_job_process(command, name=options["env"]["SIMSTACK_WINDOWS_SCIENCE_JOB"],
        cwd=options.get("cwd") or os.getcwd(), env=options["env"],
        stdout=options["stdout"], stderr=options["stderr"],
        startupinfo=options.get("startupinfo"), retain_in_child=True,
        creationflags=options.get("creationflags", 0))


def _native_cleanup(process):
    """Bind exit replay to the original owned session or Windows Job."""
    group = getattr(process, "_simstack_process_group", False)
    job = getattr(process, "_simstack_windows_job", None)
    if not group and job is None:
        return None
    from simstack.runner_runtime.pending_execution_failures import cleanup_scope
    return {"scope": cleanup_scope(), "process_group": process.pid if group else None,
            "windows_job": getattr(job, "name", None)}


@dataclass(frozen=True)
class NodeExecutionOutcome:
    success: bool
    return_kind: str


def _return_kind(result: Any, registry_entry: NodeRegistry) -> str:
    if isinstance(result, bool):
        return "bool"
    if isinstance(result, SimstackResult):
        return "multiple"
    if isinstance(result, Model):
        return "model"
    if result is None and registry_entry.status == TaskStatus.COMPLETED:
        if len(registry_entry.results_references) > 1:
            return "multiple"
        if registry_entry.results_references:
            return "model"
        return "bool"
    return "none"


async def run_node_from_registry_with_outcome(
    registry_entry: NodeRegistry,
    *, process_id: int | None = None,
) -> NodeExecutionOutcome:
    from simstack.runner_runtime.launch_claims import task_execution, current_execution, acknowledge_start
    with task_execution(registry_entry):
        execution = current_execution()
        if execution:
            from simstack.runner_runtime.native_observations import adopt
            adopt(context.server, registry_entry)
            from simstack.server_api.http_transport import RunnerAPIError
            try:
                started = await context.server.tasks.start_attempt(
                    registry_entry,
                    process_id=None if getattr(context, "in_docker", False) else process_id if process_id is not None else os.getpid(),
                )
            except RunnerAPIError as exc:
                if exc.status_code != 409:
                    raise
                # A denied executor has no scientific failure to publish.
                # In particular it must not fail an already admitted process.
                logger.warning("Task %s rejected this executor: %s", registry_entry.id, exc)
                return NodeExecutionOutcome(False, "none")
            acknowledge_start(context.server, registry_entry, started)
            if started is None:
                return NodeExecutionOutcome(False, "none")
        return await _run_node_from_registry_with_outcome(registry_entry, process_id=process_id)


async def _run_node_from_registry_with_outcome(
    registry_entry: NodeRegistry,
    *, process_id: int | None = None,
) -> NodeExecutionOutcome:
    """
    Executes a node task from the provided registry entry and updates its status
    based on the success or failure of execution.
    This always leads to local execution.

    Parameters:
        registry_entry (NodeRegistry): The registry entry containing information
        about the node execution target.

    Returns:
        bool: True if the node execution completes successfully, False otherwise.

    Raises:
        AssertionError: If the registry entry associated with the node becomes None after execution.

    """
    # Create the node from the registry entry
    node = await node_from_task(registry_entry)
    if not node:
        error = registry_entry.error or sanitized_tail(
            f"Failed to create node {registry_entry.func_mapping}",
            None,
        )
        logger.error(
            "Failed to create node from registry entry task_id: %s on "
            "resource %s: %s",
            registry_entry.id,
            context.config.resource,
            error,
        )
        return NodeExecutionOutcome(False, "exception")
    registry_entry = node.registry_entry  # it may have changed
    assert registry_entry is not None
    fresh = await context.server.tasks.get_outcome(registry_entry)
    if fresh is not None:
        node.registry_entry = fresh
        registry_entry = fresh
    if node.status == TaskStatus.TERMINATING:
        logger.info(
            "task_id: %s skipping terminating task: %s",
            registry_entry.id,
            registry_entry.name,
        )
        return NodeExecutionOutcome(False, registry_entry.return_kind or "none")
    if node.status == TaskStatus.FAILED:
        logger.info(
            "task_id: %s skipping failed task: %s",
            registry_entry.id,
            registry_entry.name,
        )
    if (
        node.status == TaskStatus.RETRIEVED
        or node.status == TaskStatus.SUBMITTED
        or node.status == TaskStatus.SLURM_QUEUED
        or node.status == TaskStatus.RUNNING
        or node.status == TaskStatus.SLURM_RUNNING
    ) or (
        node.status == TaskStatus.COMPLETED and registry_entry.parameters.force_rerun
    ):
        result = await node.execute_node_locally(process_id=process_id)
    else:
        result = None
        logger.info(
            f"task_id: {registry_entry.id} skipping task: {registry_entry.name} with status {registry_entry.status}"
        )
    return_kind = getattr(node, "_execution_return_kind", None)
    if return_kind is None:
        return_kind = _return_kind(result, registry_entry)
    return NodeExecutionOutcome(node.status == TaskStatus.COMPLETED, return_kind)


async def run_node_from_registry(registry_entry: NodeRegistry) -> bool:
    """Execute a registry entry while preserving the existing boolean API."""
    return (await run_node_from_registry_with_outcome(registry_entry)).success


class NodeExecutionService(BaseService):
    def __init__(
        self,
        resource: Resource,
        interval: int,
        max_concurrent: int,
        shutdown_event: asyncio.Event | None,
        detach: bool = True,
        is_default: bool = False,
        claiming_paused: asyncio.Event | None = None,
    ) -> None:
        super().__init__(
            "JobPolling", resource, interval, shutdown_event=shutdown_event
        )
        self._resource_name = str(resource)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running_tasks: set[asyncio.Task[bool]] = set()
        self._active_ids: set[str] = set()
        self._launch_slots = set()
        self._observers: set[asyncio.Task] = set()
        self._live_observations: set[str] = set()
        self._native_processes = {}
        self._max_concurrent = max_concurrent
        self._started = False
        self._detach = detach
        self._is_default = is_default
        self._claiming_paused = claiming_paused

    async def run_node(self, registry_entry: NodeRegistry) -> bool:
        """Execute a Server-authorized task snapshot"""

        await self.write_node_event(RunnerEventEnum.NODE_STARTED, registry_entry.id)
        phase = "launch"
        exit_code = None
        process = None
        operation = None
        observation = None
        observer_started = False
        foreground_exit = False
        foreground_error = None
        foreground_pending = None
        native_cleaned = False
        try:
            from simstack.runner_runtime.execution_environment import prepare_task
            await prepare_task(registry_entry)

            queue = (
                registry_entry.parameters.queue
                if hasattr(registry_entry.parameters, "queue")
                else "default"
            )
            if queue is None:
                logger.error(
                    f"Queue parameter not found for task_id: {registry_entry.id}"
                )
                return False

            logger.info(
                f"Running node task_id: {registry_entry.id} on resource {context.config.resource} with {queue} queue and docker: {registry_entry.parameters.in_docker} "
            )

            if queue == "slurm-queue":
                return await submit_node(registry_entry)

            if queue == "default" and registry_entry.parameters.in_docker:
                def observe_container_process(process, value):
                    from simstack.runner_runtime.native_observations import execution_key
                    key = execution_key(value["execution"])
                    self._native_processes[key] = process
                    self._live_observations.add(value["observation_id"])
                    operation.add_done_callback(lambda _task: self._native_processes.pop(key, None))
                    operation.add_done_callback(lambda _task: self._live_observations.discard(value["observation_id"]))

                operation = asyncio.create_task(run_docker(registry_entry,
                    on_observed_process=observe_container_process,
                    retry_unpersisted_evidence=self._detach))
                self._observers.add(operation)
                await self._wait_for_start(registry_entry, operation=operation)
                return await operation if not self._detach or operation.done() else True

            if queue == "default":
                # Managed science always owns a supervised process, including foreground mode.
                from simstack.runner_runtime.execution_environment import task_project_root, task_environment, task_launch_command
                project_root = str(task_project_root(registry_entry, context.config.project_root))
                cmd = task_launch_command(registry_entry, self._resource_name, project_root)

                creationflags = 0
                if platform.system() == "Windows":
                    creationflags = getattr(
                        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                    ) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

                python_path_entries = [project_root]
                python_path_entries.extend(
                    str(path)
                    for path in (
                        getattr(context.config, "python_paths", None) or []
                    )
                )
                if os.environ.get("PYTHONPATH"):
                    python_path_entries.append(os.environ["PYTHONPATH"])
                child_python_path = os.pathsep.join(
                    dict.fromkeys(
                        path for path in python_path_entries if path
                    )
                )

                try:
                    from simstack.runner_runtime.launch_claims import inherit_launch_reservation
                    from simstack.runner_runtime import native_observations
                    observation, observation_lease = native_observations.prepare(context.server, registry_entry)
                    self._live_observations.add(observation["observation_id"])
                    environment = {**task_environment(registry_entry, server=context.server), "PYTHONPATH": child_python_path}
                    environment["SIMSTACK_NATIVE_OBSERVATION_ID"] = observation["observation_id"]
                    environment["SIMSTACK_NATIVE_OBSERVATION_EXECUTION"] = json.dumps(observation["execution"])
                    if observation.get("windows_job"):
                        environment["SIMSTACK_WINDOWS_SCIENCE_JOB"] = observation["windows_job"]
                    with observation_lease:
                        with inherit_launch_reservation(getattr(registry_entry, "_launch_reservation", None), environment) as ownership:
                            with native_observations.inherit(observation_lease, ownership) as ownership:
                                process = await _spawn_native_process(
                                    *cmd,
                                    stdout=asyncio.subprocess.DEVNULL,
                                    stderr=asyncio.subprocess.DEVNULL,
                                    creationflags=creationflags,
                                    start_new_session=platform.system() != "Windows",
                                    env=environment,
                                    **ownership,
                                )
                                # Keep group ownership before any journal write or context exit can fail.
                                if platform.system() != "Windows" and isinstance(process, asyncio.subprocess.Process):
                                    process._simstack_process_group = True
                    native_observations.spawned(context.server, observation, process.pid)
                    process_key = native_observations.execution_key(observation["execution"])
                    self._native_processes[process_key] = process
                except Exception as exc:
                    error = sanitized_tail(
                        str(exc),
                        None,
                    )
                    logger.error(
                        "Failed to spawn detached process for task_id: %s. "
                        "Command: %s. Error: %s",
                        registry_entry.id,
                        " ".join(
                            sanitized_command(
                                cmd,
                                None,
                            )
                        ),
                        error,
                    )
                    raise

                logger.info(
                    f"Spawned detached process for task_id: {registry_entry.id} with PID: {process.pid}"
                )
                from simstack.runner_runtime.launch_claims import record_spawn
                record_spawn(context.server, registry_entry, process.pid)
                phase = "subprocess"
                try:
                    await record_process_id(registry_entry, process.pid, context.server)
                except Exception:
                    logger.warning("Task %s process report awaits reconciliation", registry_entry.id, exc_info=True)
                await self._wait_for_start(registry_entry, process)
                if not self._detach:
                    exit_code = await process.wait()
                    foreground_exit = bool(exit_code)
                    if foreground_exit:
                        from simstack.runner_runtime.pending_execution_failures import record_observed_failure
                        foreground_pending = record_observed_failure(
                            f"run_node child exited (code {exit_code})", phase="subprocess",
                            execution=context.server.tasks.execution(registry_entry),
                            server=context.server, resource=self._resource,
                            exit_code=exit_code if exit_code >= 0 else None,
                            signal=-exit_code if exit_code < 0 else None,
                            cleanup=_native_cleanup(process))
                    foreground_pending = await self._cleanup_native_process(
                        registry_entry, process, server=context.server,
                        workdir=getattr(context.config, "workdir", None), pending=foreground_pending)
                    native_cleaned = True
                    completed = await context.server.tasks.get_outcome(registry_entry)
                    if (getattr(completed, "_execution_native_controller", False)
                            and completed.status in {TaskStatus.COMPLETED, TaskStatus.FAILED,
                                TaskStatus.TIME_OUT, TaskStatus.TERMINATING}):
                        await context.server.tasks.termination_confirmed(registry_entry,
                            completed.error or "Scientific execution stopped")
                    if completed is not None and completed.status in {
                        TaskStatus.RETRIEVED, TaskStatus.SUBMITTED, TaskStatus.RUNNING,
                    }:
                        raise RuntimeError(completed.error or
                            f"run_node child exited without completing the task (code {process.returncode})")
                    return completed is not None and completed.status == TaskStatus.COMPLETED
                observer = asyncio.create_task(self._observe_process(registry_entry, process, server=context.server,
                    workdir=getattr(context.config, "workdir", None), observation=observation))
                observer_started = True
                observer.add_done_callback(lambda _task, identity=observation["observation_id"]:
                    self._live_observations.discard(identity))
                observer.add_done_callback(lambda _task, key=process_key: self._native_processes.pop(key, None))
                self._observers.add(observer)
                return True
            else:
                logger.error(
                    f"Queue {queue} not supported {registry_entry.name} for task_id: {registry_entry.id}"
                )
                raise RuntimeError(f"Queue {queue} not supported for task_id: {registry_entry.id}")

        except (Exception, SystemExit) as exc:
            if operation is not None and not operation.done():
                # The Docker coroutine owns its CLI/container cleanup. A
                # failed bootstrap poll cannot terminalize it while still live.
                try:
                    return await operation
                except Exception:
                    logger.warning("Task %s container outcome failed", registry_entry.id, exc_info=True)
            if process is not None and not native_cleaned:
                foreground_pending = await self._cleanup_native_process(
                    registry_entry, process, server=context.server,
                    workdir=getattr(context.config, "workdir", None), pending=foreground_pending)
                exit_code = process.returncode
            error = sanitized_tail(
                str(exc), None
            ) or type(exc).__name__
            if foreground_exit:
                foreground_error = error
            logger.error(
                "Error running node task_id: %s on resource %s: %s",
                registry_entry.id,
                context.config.resource,
                error,
            )
            if registry_entry:
                from simstack.server_api.http_transport import RunnerAPIError
                termination = {"exit_code": exit_code if exit_code is None or exit_code >= 0 else None,
                               "signal": -exit_code if exit_code is not None and exit_code < 0 else None}
                try:
                    failed = await context.server.tasks.fail_attempt(registry_entry, error, phase=phase, **termination)
                    if native_cleaned and getattr(failed, "_execution_native_controller", False):
                        await context.server.tasks.termination_confirmed(registry_entry,
                            failed.error or "Scientific execution stopped")
                except (RunnerAPIError, OSError) as failure:
                    from simstack.runner_runtime.pending_execution_failures import record_failure
                    try:
                        if not foreground_exit:
                            record_failure(error, phase=phase, execution=context.server.tasks.execution(registry_entry),
                                server=context.server, resource=self._resource,
                                rejected_status=getattr(failure, "status_code", None), **termination)
                    except OSError:
                        logger.warning("Could not journal task %s failure: %s", registry_entry.id, error, exc_info=True)
                    logger.warning("Task %s failure could not be acknowledged", registry_entry.id, exc_info=True)
            return False
        finally:
            if foreground_exit:
                from simstack.runner_runtime.pending_execution_failures import (
                    record_failure, reconcile_observed_failure,
                )
                if foreground_error:
                    # Preserve the foreground failure cause without losing the
                    # exit receipt already durable before the first API read.
                    foreground_pending[1]["error"] = foreground_error
                    if foreground_pending[0] is not None:
                        try:
                            record_failure(server=context.server, resource=self._resource,
                                           **foreground_pending[1])
                        except OSError:
                            logger.warning("Could not update task %s exit cause", registry_entry.id, exc_info=True)
                await reconcile_observed_failure(context.server, foreground_pending)
            if observation is not None and not observer_started:
                self._live_observations.discard(observation["observation_id"])
                self._native_processes.pop(native_observations.execution_key(observation["execution"]), None)

    async def execute(self) -> None:
        if not self._started:
            await self.write_resource_event(RunnerEventEnum.RUNNER_STARTED)
            self._started = True

        for observer in tuple(self._observers):
            if observer.done():
                self._observers.remove(observer)
                try:
                    await observer
                except Exception:
                    logger.exception("Task outcome observer failed")

        # Clean up the completed tasks
        completed_tasks = {task for task in self._running_tasks if task.done()}
        for task in completed_tasks:
            try:
                await task
            except Exception as e:
                logger.exception(f"Task completed with error: {e}")
            self._running_tasks.remove(task)

        from simstack.runner_runtime.pending_cancellations import reconcile_cancellations
        await reconcile_cancellations(context.server)
        from simstack.runner_runtime.pending_execution_failures import reconcile_failures
        await reconcile_failures(context.server, resource=self._resource)
        from simstack.runner_runtime.native_observations import recover, execution_key
        retained_observations = await recover(context.server, live=self._live_observations)
        await self._reconcile_started_containers()
        terminating_tasks = await context.server.tasks.terminating_tasks()
        for entry in terminating_tasks:
            from simstack.core.services.task_termination import TaskTerminationError
            try:
                key = execution_key(context.server.tasks.execution(entry))
                if key in retained_observations:
                    process = self._native_processes.get(key)
                    if process is None:
                        logger.warning("Task %s awaits verified native ownership before cancellation", entry.id)
                        continue
                    from simstack.core.services.task_termination import stop_task_execution
                    await stop_task_execution(entry, server=context.server, runner_pid=self._pid,
                        workdir=getattr(context.config, "workdir", None), local_process=process)
                    await context.server.tasks.termination_confirmed(entry, entry.error or "Terminated by user")
                    continue
                await terminate_task(entry, runner_pid=self._pid)
            except TaskTerminationError as exc:
                logger.error("Task %s cancellation is not complete: %s", entry.id, exc)

        if self._claiming_paused is not None and self._claiming_paused.is_set():
            return

        from simstack.runner_runtime.launch_claims import recover_launches, reserve_launch, receipt, finish
        recovered = await recover_launches(context.server)
        available = max(0, self._max_concurrent - len(self._active_ids) - len(recovered))
        from simstack.runner_runtime.launch_claims import acquire_work
        acquired = await acquire_work(context.server, capacity=available) if available else []
        for entry in [*recovered, *acquired]:
            task_id = str(entry.id)
            if task_id in self._active_ids:
                reservation = getattr(entry, "_launch_reservation", None)
                if reservation is not None:
                    reservation.close()
                continue
            reservation = getattr(entry, "_launch_reservation", None)
            if reservation is None:
                reservation = reserve_launch(context.server, task_id)
                if reservation is None:
                    continue
                value = receipt(context.server, task_id, begin=True,
                    generation=getattr(entry, "_execution_generation", None),
                    claim_id=getattr(entry, "_execution_claim_id", None),
                    revision=entry.repository_revision)
                finish(context.server, value, claimed=True)
                object.__setattr__(entry, "_launch_receipt", value)
                object.__setattr__(entry, "_launch_reservation", reservation)
            self._active_ids.add(task_id)
            task = asyncio.create_task(self._run_with_semaphore(entry))
            self._running_tasks.add(task)

    async def _reconcile_started_containers(self):
        """Recover outcomes from exact daemon objects after their Runner exits."""
        from simstack.core.run_docker import docker_execution_name, inspect_docker_execution, format_container_failure_error
        from simstack.server_api.http_transport import RunnerAPIError
        from simstack.runner_runtime.pending_execution_failures import record_failure, acknowledge_failure

        for entry in await context.server.resource.active_containers():
            if (entry.parameters.queue != "default"
                    or getattr(entry, "_execution_container_backend", None) not in {None, "docker"}
                    or not getattr(entry, "_execution_start_id", None)):
                continue
            try:
                name = docker_execution_name(context.server, context.server.tasks.execution(entry))
                observed = await asyncio.to_thread(inspect_docker_execution, name)
                # An absent object can belong to another host/daemon. Neither
                # absence nor daemon failure establishes a stopped execution.
                if observed is None or observed["State"]["Status"] not in {"exited", "dead"}:
                    continue
                state = observed["State"]
                code = state["ExitCode"]
                error = (format_container_failure_error(code, oom_killed=state.get("OOMKilled", False))
                    if code else "Container exited without recording an application outcome")

                def retain_evidence(message):
                    try:
                        return record_failure(message, phase="container", exit_code=code,
                            execution=context.server.tasks.execution(entry), server=context.server,
                            resource=self._resource)
                    except OSError:
                        # A broken local journal must not prevent a reachable
                        # Server from accepting this exact physical outcome.
                        logger.warning("Task %s could not journal its Docker outcome", entry.id, exc_info=True)
                        return None

                # Completion can win the API read/fail race and remove this
                # task from the watchlist. Retain physical exit evidence first.
                pending = retain_evidence(error) if code else None
                current = await context.server.tasks.get_outcome(entry)
                if current is not None and current.status == TaskStatus.TERMINATING:
                    if not code:
                        pending = retain_evidence(current.error or "Container stopped during cancellation")
                    await context.server.tasks.termination_confirmed(entry, current.error or "Terminated by user")
                    # The ACK records cancellation; journal reconciliation also
                    # retains the actual exit without replacing its primary text.
                elif current is None or current.status in {
                        TaskStatus.RETRIEVED, TaskStatus.SUBMITTED, TaskStatus.RUNNING}:
                    if not code:
                        pending = retain_evidence(error)
                    await context.server.tasks.fail_attempt(entry, error, phase="container", exit_code=code)
                    if pending is not None:
                        acknowledge_failure(pending)
                # A terminal application outcome stays authoritative. Nonzero
                # physical exit evidence is delivered by the next journal pass;
                # completed/zero has no failure evidence to invent.
            except (RunnerAPIError, OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                logger.warning("Task %s awaits exact Docker outcome reconciliation", entry.id, exc_info=True)

    async def _wait_for_start(self, entry, process=None, *, operation=None):
        """Reserve capacity through bootstrap, while retrying recovered API reads."""
        from simstack.runner_runtime.launch_claims import acknowledge_start
        from simstack.server_api.http_transport import RunnerAPIError
        expected = getattr(entry, "_execution_start_id", None)
        while True:
            if operation is not None and operation.done():
                await operation
                return
            try:
                canonical = await context.server.tasks.get_task(entry.id)
            except (RunnerAPIError, OSError) as exc:
                if isinstance(exc, RunnerAPIError) and exc.status_code is not None and exc.status_code < 500:
                    raise
                if process is not None and process.returncode is not None:
                    return  # The outcome observer retains the stopped handle.
                await asyncio.sleep(0.1)
                continue
            if canonical is None:
                raise RuntimeError(f"Task {entry.id} disappeared during launch")
            start = getattr(canonical, "_execution_start_id", None)
            if start is not None:
                if start != expected:
                    raise RuntimeError(f"Task {entry.id} has a competing start")
                acknowledge_start(context.server, entry, canonical)
                self._release_launch_slot(entry)
                return
            if canonical.status not in {TaskStatus.RETRIEVED, TaskStatus.SUBMITTED}:
                return
            if process is not None and process.returncode is not None:
                return
            await asyncio.sleep(0.1)

    async def _cleanup_native_process(self, entry, process, *, server, workdir, pending=None):
        from simstack.core.services.task_termination import stop_task_execution, TaskTerminationError
        from simstack.server_api.http_transport import RunnerAPIError
        if process.returncode is None or _native_cleanup(process) is not None:
            while True:
                try:
                    await stop_task_execution(entry, server=server, runner_pid=self._pid,
                        workdir=workdir, local_process=process)
                    break
                except (RunnerAPIError, OSError, TaskTerminationError, subprocess.SubprocessError):
                    logger.warning("Task %s retains its native handle until physical cleanup is verified", entry.id, exc_info=True)
                    await asyncio.sleep(1)
        if pending is not None and "cleanup" in pending[1]:
            from simstack.runner_runtime.pending_execution_failures import record_failure, reconcile_observed_failure
            path, outcome = pending
            outcome = {key: value for key, value in outcome.items() if key != "cleanup"}
            # Persist verified cleanup before releasing the owner, including
            # Windows Jobs that can disappear after the last handle closes.
            while True:
                try:
                    path = record_failure(server=server, resource=self._resource, **outcome)
                    return path, outcome
                except OSError:
                    if await reconcile_observed_failure(server, (path, outcome)):
                        return None, outcome
                    logger.warning("Task %s retains its verified native exit until it is durable", entry.id, exc_info=True)
                    await asyncio.sleep(1)
        return pending

    async def _observe_process(self, entry, process, *, server, workdir, observation=None):
        """Keep the real worker handle after bootstrap releases launch capacity."""
        from simstack.server_api.http_transport import RunnerAPIError
        from simstack.runner_runtime.pending_execution_failures import (
            record_failure, record_observed_failure, reconcile_observed_failure,
        )
        code = await process.wait()
        termination = {"exit_code": code if code >= 0 else None,
                       "signal": -code if code < 0 else None}
        pending = (record_observed_failure(f"run_node child exited (code {code})",
            phase="subprocess", execution=server.tasks.execution(entry), server=server,
            resource=self._resource, cleanup=_native_cleanup(process), **termination) if code else None)
        pending = await self._cleanup_native_process(entry, process, server=server, workdir=workdir, pending=pending)
        error = f"run_node child exited without recording its outcome (code {code})"
        journaled = pending is not None and pending[0] is not None
        while True:
            try:
                current = await server.tasks.get_outcome(entry)
                if current is None or current.status in {
                    TaskStatus.RETRIEVED, TaskStatus.SUBMITTED, TaskStatus.RUNNING,
                    TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING,
                }:
                    current = await server.tasks.fail_attempt(entry, error, phase="subprocess", **termination)
                if getattr(current, "_execution_native_controller", False):
                    await server.tasks.termination_confirmed(entry, current.error or "Scientific execution stopped")
                if pending is not None:
                    delivered = await reconcile_observed_failure(server, pending)
                    if not delivered and pending[0] is None:
                        # Both durable sinks failed. This observer still owns
                        # the only exact receipt and must not discard it.
                        await asyncio.sleep(1)
                        continue
                if observation is not None and current is not None and current.status != TaskStatus.TERMINATING:
                    from simstack.runner_runtime.native_observations import retire
                    retire(server, observation)
                return
            except (RunnerAPIError, OSError) as exc:
                if not journaled:
                    try:
                        if pending is not None:
                            path = record_failure(server=server, resource=self._resource, **pending[1])
                            pending = path, pending[1]
                        else:
                            path = record_failure(error, phase="subprocess", execution=server.tasks.execution(entry),
                                server=server, resource=self._resource,
                                rejected_status=getattr(exc, "status_code", None), **termination)
                        journaled = path is not None
                    except OSError:
                        logger.warning("Task %s observer retains its unpersisted exit", entry.id, exc_info=True)
                if pending is not None:
                    await reconcile_observed_failure(server, pending)
                # Accepted exit evidence does not replace the physical stop ACK.
                # Retain the observer through a transient read/ACK outage.
                if (isinstance(exc, RunnerAPIError) and exc.status_code is not None and exc.status_code < 500
                        and (pending is None or journaled)):
                    return
                await asyncio.sleep(1)

    def _release_launch_slot(self, entry):
        if getattr(entry, "_launch_slot_released", False):
            return
        object.__setattr__(entry, "_launch_slot_released", True)
        if id(entry) in self._launch_slots:
            self._launch_slots.remove(id(entry))
            self._semaphore.release()
        self._active_ids.discard(str(entry.id))
        reservation = getattr(entry, "_launch_reservation", None)
        if reservation is not None:
            reservation.close()
            object.__setattr__(entry, "_launch_reservation", None)

    async def _run_with_semaphore(self, entry: NodeRegistry) -> bool:
        object.__setattr__(entry, "_launch_slot_released", False)
        try:
            await self._semaphore.acquire()
            self._launch_slots.add(id(entry))
            from simstack.runner_runtime.launch_claims import task_execution
            with task_execution(entry):
                return await self.run_node(entry)
        finally:
            self._release_launch_slot(entry)
