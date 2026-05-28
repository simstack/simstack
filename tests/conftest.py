import pytest
import asyncio


@pytest.fixture(scope="session")
def event_loop():
    # This event_loop is required because the default pytest-asyncio event loop is function scoped
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    # loop.close()
