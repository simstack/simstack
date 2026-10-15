import pytest
from simstack.core.context import context


@pytest.mark.asyncio
async def test_initialized_context(repository):
    """text whether the context works"""
    assert context.initialized is True