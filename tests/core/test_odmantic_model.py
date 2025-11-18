# test_file.py
import pytest
from odmantic import Model
from typing import Optional


class SampleModel(Model):
    name: str
    value: int
    description: Optional[str] = None


@pytest.mark.asyncio
async def test_odmantic_save(odmantic_engine):
    # Create and save a document
    test_instance = SampleModel(name="test1", value=42, description="Test document")
    inserted = await odmantic_engine.save(test_instance)
    assert inserted.id is not None


@pytest.mark.asyncio
async def test_odmantic_find(odmantic_engine):
    # Create and save a document
    test_instance = SampleModel(name="test2", value=43, description="Another document")
    await odmantic_engine.save(test_instance)

    # Find the document
    result = await odmantic_engine.find_one(SampleModel, SampleModel.name == "test2")
    assert result is not None
    assert result.value == 43
