"""Consume an optional task CodeSource at the runner boundary."""

from pathlib import Path
from typing import Any, Callable, Optional, Type

from odmantic import Model

from simstack.core.workflow_repository import cached_repository_checkout
from simstack.models import NodeRegistry
from simstack.util.importer import import_class, import_function


async def materialize_task_checkout(db: Any, task: NodeRegistry) -> Path | None:
    """Materialize the task's exact commit, or preserve the installed-code path."""

    if task.code_source is None:
        return None
    return await cached_repository_checkout(db, task.code_source)


async def import_task_function(
    db: Any,
    task: NodeRegistry,
    *,
    tolerate_missing_function: bool = False,
) -> Optional[Callable[..., Any]]:
    return await import_function(
        task.func_mapping,
        db,
        task_id=task.id,
        tolerate_missing_function=tolerate_missing_function,
        code_source=task.code_source,
    )


async def import_task_model(
    db: Any,
    task: NodeRegistry,
    model_mapping: str,
) -> Type[Model] | None:
    return await import_class(
        model_mapping,
        db,
        code_source=task.code_source,
    )


def task_code_source_log_fields(task: NodeRegistry) -> dict[str, str | None]:
    if task.code_source is None:
        return {"repo_id": None, "commit": None}
    return {
        "repo_id": str(task.code_source.repo_id),
        "commit": task.code_source.commit,
    }
