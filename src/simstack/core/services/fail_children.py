"""Fail and stop child tasks when their parent fails."""

import logging
from asyncio.subprocess import Process

from bson import ObjectId as BsonObjectId
from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.services.task_termination import PARENT_STOP_PREFIX, terminate_task
from simstack.core.services.task_termination import _kill_process_tree as kill_process_tree
from simstack.models import NodeRegistry

logger = logging.getLogger("NodeRunner")

_ACTIVE_STATUSES = tuple(
    status.value
    for status in (
        TaskStatus.SUBMITTED,
        TaskStatus.RETRIEVED,
        TaskStatus.RUNNING,
        TaskStatus.SLURM_QUEUED,
        TaskStatus.SLURM_RUNNING,
    )
)


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


async def _claim_child_stop(entry: NodeRegistry, parent_id: ObjectId | None, error: str) -> bool:
    """Reserve the stop only if no parent joined since the check."""
    if entry.status not in _ACTIVE_STATUSES:
        return False
    for other_id in entry.parent_ids:
        if other_id == parent_id:
            continue
        other = await context.db.load_task_by_id(other_id)
        if other is not None and (
            other.status in _ACTIVE_STATUSES or other.status == TaskStatus.TERMINATING
        ):
            logger.info("Task task_id: %s still needed by workflow %s", entry.id, other_id)
            return False

    # Another workflow can join after we check the parents. Match the saved
    # parent list in the same write that marks the child as stopping.
    collection = context.db.get_collection(NodeRegistry)
    updated = await collection.find_one_and_update(
        {
            "_id": entry.id,
            "parent_ids": list(entry.parent_ids),
            "status": {"$in": _ACTIVE_STATUSES},
        },
        {"$set": {"status": TaskStatus.TERMINATING.value, "error": error}},
    )
    return updated is not None


async def fail_and_terminate_task(
    entry: NodeRegistry,
    *,
    error: str,
    runner_pid: int,
    parent_id: ObjectId | None = None,
    local_process: Process | None = None,
) -> bool:
    """Request a child stop and return whether this caller stopped it locally."""
    if entry is None or entry.id is None:
        raise ValueError("task registry entry is required")
    if not error.strip():
        raise ValueError("error message is required")
    try:
        if not await _claim_child_stop(entry, parent_id, error):
            return False
    except Exception:
        logger.exception("Task task_id: %s could not record the child stop request", entry.id)
        return False

    logger.info("Task task_id: %s terminating with %s", entry.id, error)
    # Named workers poll for stop requests. A self task runs inside its parent.
    if entry.parameters.resource != "self":
        return False

    stopped_pid = None
    if local_process is not None and local_process.pid != runner_pid:
        if local_process.returncode is None and not kill_process_tree(local_process.pid):
            return False
        stopped_pid = local_process.pid
    try:
        finished = await terminate_task(
            entry,
            runner_pid=runner_pid,
            locally_stopped_pid=stopped_pid,
            expected_error=error,
        )
        return finished or stopped_pid is not None
    except Exception:
        logger.exception("Task task_id: %s could not finish local child cleanup", entry.id)
        return stopped_pid is not None


async def fail_and_terminate_children(
    parent_id: object,
    *,
    runner_pid: int,
) -> None:
    """Fail and stop tasks whose parent_ids include this parent."""
    parent = _parent_object_id(parent_id)
    error = f"{PARENT_STOP_PREFIX}parent task {parent} failed"
    children = await context.db.find(NodeRegistry, NodeRegistry.parent_ids.in_([parent]))
    if children is None:
        raise ValueError(f"child lookup for task_id: {parent} returned None")
    for child in children:
        await fail_and_terminate_task(
            child,
            error=error,
            runner_pid=runner_pid,
            parent_id=parent,
        )


async def fail_abandoned_child(
    entry: NodeRegistry,
    *,
    parent_id: object,
    runner_pid: int,
    local_process: Process | None = None,
) -> bool:
    """Fail a child whose parent stopped waiting. Logs and swallows termination errors."""
    if parent_id is None:
        error = f"{PARENT_STOP_PREFIX}the parent task failed"
    else:
        error = f"{PARENT_STOP_PREFIX}parent task {parent_id} failed"
    try:
        return await fail_and_terminate_task(
            entry,
            error=error,
            runner_pid=runner_pid,
            parent_id=_parent_object_id(parent_id) if parent_id is not None else None,
            local_process=local_process,
        )
    except Exception:
        logger.exception(
            "Task task_id: %s could not be terminated after the parent failed",
            getattr(entry, "id", None),
        )
        return False
