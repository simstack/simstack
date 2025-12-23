import pytest
import asyncio


@pytest.fixture(scope="session")
def event_loop():
    # This event_loop is required because the default pytest-asyncio event loop is function scoped
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    # loop.close()
