from __future__ import annotations

from typing import Optional

import pytest

from simstack.models.document import Model
from simstack.persistence import eq


class SampleModel(Model):
    name: str
    value: int
    description: Optional[str] = None


@pytest.mark.asyncio
async def test_repository_save(repository):
    instance = SampleModel(name="test1", value=42, description="Test document")

    inserted = await repository.save(instance)

    assert inserted.id is not None


@pytest.mark.asyncio
async def test_repository_find(repository):
    instance = SampleModel(name="test2", value=43, description="Another document")
    await repository.save(instance)

    result = await repository.find_one(SampleModel, eq("name", "test2"))

    assert result is not None
    assert result.value == 43
