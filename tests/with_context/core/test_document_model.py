# test_file.py
import pytest
from simstack.models.document import Model
from typing import Optional


class SampleModel(Model):
    name: str
    value: int
    description: Optional[str] = None


@pytest.mark.asyncio
async def test_document_save(initialized_context):
    # Create and save a document
    test_instance = SampleModel(name="test1", value=42, description="Test document")
    inserted = await initialized_context.server.values.publish_value(test_instance)
    assert inserted.id is not None


@pytest.mark.asyncio
async def test_document_find(initialized_context):
    # Create and save a document
    test_instance = SampleModel(name="test2", value=43, description="Another document")
    await initialized_context.server.values.publish_value(test_instance)

    # Find the document
    result = (await initialized_context.server.values.query_records(SampleModel, "by_name", name="test2"))[0]
    assert result is not None
    assert result.value == 43
