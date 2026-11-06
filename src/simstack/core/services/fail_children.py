"""Fail and stop child tasks when their parent fails."""

import logging
from datetime import datetime

from simstack.models.document import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.services.task_termination import stop_mutation_values, stop_task_execution
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
    if isinstance(parent_id, ObjectId):
        return parent_id
    if isinstance(parent_id, str) and ObjectId.is_valid(parent_id):
        return ObjectId(parent_id)
    raise ValueError(
        "parent task id must be an ObjectId, got "
        f"{type(parent_id).__module__}.{type(parent_id).__qualname__} {parent_id!r}"
    )


async def _stop_task(entry: NodeRegistry, *, runner_pid: int, extra_pids: tuple[int, ...]) -> None:
    await stop_task_execution(
        entry, runner_pid=runner_pid,
        workdir=getattr(context.config, "workdir", None), extra_pids=extra_pids,
    )


async def _mark_failed_if_active(entry: NodeRegistry, error: str) -> bool:
    if not error.strip():
        raise ValueError("error message is required")
    completed_at = datetime.now()
    updated = await context.db.task_mutation(entry.id, "failure", stop_mutation_values(entry, {
        "status": TaskStatus.FAILED.value, "error": error, "completed_at": completed_at,
    }, context.db), [status.value for status in _ACTIVE_STATUSES])
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
    parent_id: object = None,
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
    from simstack.core.definitions import DBType
    if getattr(context.db, "databae_type", None) == DBType.API:
        owner = getattr(fresh, "_execution_resource", None) or str(fresh.parameters.resource)
        if owner != str(context.config.resource):
            if parent_id is None:
                raise ValueError("A parent task is required for remote cancellation")
            await context.db.task_mutation(fresh.id, "request_termination", {
                "parent_id": str(parent_id), "error": error,
            })
            return True
        if parent_id is not None:
            requested = await context.db.task_mutation(fresh.id, "request_termination", {
                "parent_id": str(parent_id), "error": error,
            })
            if requested is None:
                return False
            fresh.status = TaskStatus.TERMINATING
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
            parent_id=parent,
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
            parent_id=parent_id,
        )
    except Exception:
        logger.exception(
            "Task task_id: %s could not be terminated after the parent failed",
            getattr(entry, "id", None),
        )
