import pytest
from simstack.core.context import context


@pytest.mark.asyncio
async def test_initialized_context(odmantic_engine):
    """text whether the context works"""
    assert context.initialized is True
