"""Fail and stop child tasks when their parent fails."""

import logging
import asyncio

from simstack.models.document import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.services.task_termination import owned_local_process, stop_task_execution
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


def _parent_object_id(parent_id: object) -> ObjectId:
    parent_id = getattr(parent_id, "id", parent_id)
    if isinstance(parent_id, ObjectId):
        return parent_id
    if isinstance(parent_id, str) and ObjectId.is_valid(parent_id):
        return ObjectId(parent_id)
    raise ValueError(
        "parent task id must be an ObjectId, got "
        f"{type(parent_id).__module__}.{type(parent_id).__qualname__} {parent_id!r}"
    )


async def _stop_task(entry: NodeRegistry, *, runner_pid: int, extra_pids: tuple[int, ...], local_process=None) -> None:
    await stop_task_execution(
        entry, server=context.server, runner_pid=runner_pid,
        workdir=getattr(context.config, "workdir", None), extra_pids=extra_pids, local_process=local_process,
    )


async def _mark_failed_if_active(entry: NodeRegistry, error: str) -> bool:
    if not error.strip():
        raise ValueError("error message is required")
    if entry.status == TaskStatus.TERMINATING:
        updated = await context.server.tasks.termination_confirmed(entry, error)
    else:
        updated = await context.server.tasks.fail_attempt(entry, error, phase="termination")
    if updated is None:
        return False
    entry.status = updated.status
    entry.error = updated.error
    entry.completed_at = updated.completed_at
    return True


async def fail_and_terminate_task(
    entry: NodeRegistry,
    *,
    error: str,
    runner_pid: int,
    extra_pids: tuple[int, ...] = (),
    local_process=None,
    seen: set[ObjectId] | None = None,
    parent_id: object = None,
    parent_execution=None,
) -> bool:
    """Kill an active task, mark it failed, then do the same for its children."""
    if entry is None or entry.id is None:
        raise ValueError("task registry entry is required")
    if not error.strip():
        raise ValueError("error message is required")
    fresh = await context.server.tasks.get_task(entry.id)
    if fresh is None:
        raise ValueError(f"Task task_id: {entry.id} could not be reloaded")
    if fresh.status not in _ACTIVE_STATUSES:
        return False
    if seen is None:
        seen = set()
    if fresh.id in seen:
        return False
    seen.add(fresh.id)
    # A fresh read is for local handles only; it cannot grant authority over
    # another generation or start that raced with the original observation.
    expected_execution = context.server.tasks.execution(entry)
    fresh_execution = context.server.tasks.execution(fresh)
    for name in ("generation", "claim_id", "start_id"):
        expected = expected_execution.get(name)
        if expected is not None and expected != fresh_execution.get(name):
            return False
    owner = getattr(fresh, "_execution_resource", None) or str(fresh.parameters.resource)
    if parent_id is not None:
        if parent_execution is None:
            raise ValueError("Original parent execution is required for child cancellation")
        requested = await context.server.tasks.cancel_child(parent_id, parent_execution, fresh, error)
        if requested is None or requested.status != TaskStatus.TERMINATING:
            return False
        fresh = requested
    authorization = getattr(context.server.requests, "authorization", None)
    if local_process is None and authorization in {"user", "local"}:
        local_process = owned_local_process(fresh)
    physical_owner = getattr(fresh, "_execution_owner", None)
    if parent_id is None and physical_owner and local_process is not None:
        fresh = await context.server.tasks.request_cancellation(fresh, error)
    # SDK resources are logical routing names; only its process handle proves
    # ownership. The owning launcher or Runner observes Server cancellation.
    if local_process is None and (
        authorization in {"user", "local"}
        or owner != str(context.config.resource)
    ):
        if parent_id is None:
            raise ValueError("A parent task is required for delegated cancellation")
        return True
    logger.info(
        "Task task_id: %s terminating with %s",
        fresh.id,
        error,
    )
    await _stop_task(fresh, runner_pid=runner_pid, extra_pids=extra_pids, local_process=local_process)
    if not await _mark_failed_if_active(fresh, error):
        return False
    entry.status = TaskStatus.FAILED
    entry.error = error
    entry.completed_at = fresh.completed_at
    await fail_and_terminate_children(fresh, runner_pid=runner_pid, seen=seen)
    return True


async def fail_and_terminate_children(
    parent_id: object,
    *,
    runner_pid: int,
    seen: set[ObjectId] | None = None,
    parent_execution=None,
) -> None:
    """Fail and stop tasks whose parent_ids include this parent."""
    parent = _parent_object_id(parent_id)
    if parent_execution is None:
        if not hasattr(parent_id, "id"):
            raise ValueError("Original parent execution is required for child cancellation")
        parent_execution = context.server.tasks.execution(parent_id)
    error = f"Terminated because parent task {parent} failed"
    children = await context.server.tasks.children(parent)
    if children is None:
        raise ValueError(f"child lookup for task_id: {parent} returned None")
    for child in children:
        await fail_and_terminate_task(
            child,
            error=error,
            runner_pid=runner_pid,
            seen=seen,
            parent_id=parent,
            parent_execution=parent_execution,
        )


async def fail_abandoned_child(
    entry: NodeRegistry,
    *,
    parent_id: object,
    parent_execution=None,
    runner_pid: int,
    extra_pids: tuple[int, ...] = (),
    local_process=None,
) -> None:
    """Cancel a dependency, retaining owned handles through stop uncertainty."""
    if parent_id is None:
        error = "Terminated because the parent task failed"
    else:
        error = f"Terminated because parent task {parent_id} failed"
    from simstack.server_api.http_transport import RunnerAPIError
    from simstack.core.services.task_termination import TaskTerminationError
    if local_process is None:
        # Another waiter in this process may own the same exact launch. Its
        # handle still requires physical cleanup before cancellation completes.
        local_process = owned_local_process(entry)
    if parent_id is not None and local_process is None:
        from simstack.runner_runtime.pending_cancellations import (
            record_cancellation, replay_cancellation, retry_cancellation,
        )
        # Persist before any read or write: a timeout may be caught while its
        # parent continues running, so cancellation cannot depend on parent exit.
        path = record_cancellation(context.server, entry, parent_execution, error,
            workdir=getattr(context.config, "workdir", None))
        try:
            await replay_cancellation(context.server, path)
        except (RunnerAPIError, TaskTerminationError):
            logger.warning("Task %s cancellation retained for retry", entry.id, exc_info=True)
            retry_cancellation(context.server, path)
        except asyncio.CancelledError:
            retry_cancellation(context.server, path)
            raise
        return
    while True:
        try:
            await fail_and_terminate_task(
                entry,
                error=error,
                runner_pid=runner_pid,
                extra_pids=extra_pids,
                local_process=local_process,
                parent_id=parent_id,
                parent_execution=parent_execution,
            )
            return
        except (RunnerAPIError, TaskTerminationError):
            if local_process is not None:
                # Keep the actual handle until Server can distinguish shared
                # descendants. A failed lookup never authorizes a blind kill.
                logger.warning("Task %s awaits verified local cancellation", entry.id, exc_info=True)
                await asyncio.sleep(1)
                continue
            logger.exception("Task %s cancellation could not be delegated", entry.id)
            return
        except Exception:
            logger.exception(
                "Task task_id: %s could not be terminated after the parent failed",
                getattr(entry, "id", None),
            )
            return
