from __future__ import annotations

import pytest

from simstack.models import StringData
from simstack.persistence import Repository, ascending, eq


pytestmark = pytest.mark.asyncio


async def test_context_exposes_explicit_repository_port(
    initialized_context,
    repository,
) -> None:
    assert initialized_context.repository is repository
    assert isinstance(repository, Repository)


async def test_repository_save_find_update_and_delete(repository) -> None:
    model = StringData(value="repository-round-trip")
    saved = await repository.save(model)

    assert saved is model
    loaded = await repository.find_one(StringData, eq("id", saved.id))
    assert loaded is not None
    assert loaded.value == "repository-round-trip"

    saved.value = "repository-updated"
    await repository.save(saved)
    updated = await repository.find_one(StringData, eq("id", saved.id))
    assert updated is not None
    assert updated.value == "repository-updated"

    await repository.delete(saved)
    assert await repository.find_one(StringData, eq("id", saved.id)) is None


async def test_repository_criteria_sort_and_limit(repository) -> None:
    prefix = "criteria-sort-"
    values = [
        StringData(value=f"{prefix}c"),
        StringData(value=f"{prefix}a"),
        StringData(value=f"{prefix}b"),
    ]
    for value in values:
        await repository.save(value)

    loaded = await repository.find(
        StringData,
        sort=ascending("value"),
    )
    matching = [value.value for value in loaded if value.value.startswith(prefix)]
    assert matching == [f"{prefix}a", f"{prefix}b", f"{prefix}c"]


async def test_repository_delete_all_is_explicit(repository) -> None:
    await repository.save(StringData(value="delete-all-a"))
    await repository.save(StringData(value="delete-all-b"))

    await repository.delete_all(StringData)

    assert await repository.find(StringData) == []


async def test_repository_rejects_backend_queries(repository) -> None:
    with pytest.raises(TypeError, match="Criteria"):
        await repository.find(StringData, {"value": "raw-query"})
