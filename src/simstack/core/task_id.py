from contextvars import ContextVar
from typing import Optional

from bson.objectid import ObjectId

# this variable is meant to give access to the task id to user functions that do not have access to it (e.g. for logging)
_task_id: ContextVar[Optional[ObjectId]] = ContextVar("task_id", default=None)


def get_task_id() -> Optional[ObjectId]:
    """Get the current task ID."""
    return _task_id.get()


def set_task_id(task_id: Optional[ObjectId]) -> None:
    """Set the current task ID."""
    _task_id.set(task_id)


def clear_task_id() -> None:
    """Clear the current task ID."""
    _task_id.set(None)
