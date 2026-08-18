from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core import task_code_source


@pytest.mark.asyncio
async def test_task_checkout_preserves_legacy_path_and_materializes_repository_source(
    monkeypatch,
):
    checkout = AsyncMock(return_value=Path("/cache/exact-commit"))
    monkeypatch.setattr(task_code_source, "cached_repository_checkout", checkout)
    database = object()

    assert (
        await task_code_source.materialize_task_checkout(
            database, SimpleNamespace(code_source=None)
        )
        is None
    )
    checkout.assert_not_awaited()

    source = SimpleNamespace(repo_id="repo-a", commit="abc123")
    task = SimpleNamespace(code_source=source)
    assert await task_code_source.materialize_task_checkout(
        database, task
    ) == Path("/cache/exact-commit")
    checkout.assert_awaited_once_with(database, source)


@pytest.mark.asyncio
async def test_task_imports_forward_one_optional_code_source(monkeypatch):
    imported_function = AsyncMock(return_value=lambda: None)
    imported_model = AsyncMock(return_value=object)
    monkeypatch.setattr(task_code_source, "import_function", imported_function)
    monkeypatch.setattr(task_code_source, "import_class", imported_model)
    database = object()
    source = SimpleNamespace(repo_id="repo-a", commit="abc123")
    task = SimpleNamespace(
        id="task-a",
        func_mapping="workflow.run",
        code_source=source,
    )

    await task_code_source.import_task_function(database, task)
    await task_code_source.import_task_model(database, task, "workflow.Input")

    imported_function.assert_awaited_once_with(
        "workflow.run",
        database,
        task_id="task-a",
        tolerate_missing_function=False,
        code_source=source,
    )
    imported_model.assert_awaited_once_with(
        "workflow.Input",
        database,
        code_source=source,
    )


def test_task_code_source_log_fields_are_consistent():
    assert task_code_source.task_code_source_log_fields(
        SimpleNamespace(code_source=None)
    ) == {"repo_id": None, "commit": None}
    assert task_code_source.task_code_source_log_fields(
        SimpleNamespace(code_source=SimpleNamespace(repo_id="repo-a", commit="abc123"))
    ) == {"repo_id": "repo-a", "commit": "abc123"}
