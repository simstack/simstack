import asyncio
from unittest.mock import AsyncMock

import pytest

from simstack.core.services.timeout_restart_service import TimeoutRestartService
from simstack.models.parameters import Resource


@pytest.mark.asyncio
async def test_timeout_waits_before_shutdown_and_emits_event(monkeypatch):
    shutdown = asyncio.Event()
    service = TimeoutRestartService(
        Resource(value="resource"), timeout_minutes=1, shutdown_event=shutdown
    )
    service.write_resource_event = AsyncMock()
    waiting = asyncio.Event()
    deadline = asyncio.Event()
    actual_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        if timeout == 60 and not service._executed:
            # Controlled clock: the deadline has not elapsed until released.
            awaitable.close()
            waiting.set()
            await deadline.wait()
            raise asyncio.TimeoutError
        return await actual_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    task = service.start()
    await waiting.wait()
    assert not shutdown.is_set()
    service.write_resource_event.assert_not_awaited()

    deadline.set()
    await task
    assert shutdown.is_set()
    assert service._stop_event.is_set()
    service.write_resource_event.assert_awaited_once()
    assert service.write_resource_event.await_args.kwargs["message"] == (
        "Timeout restart after 1 minutes"
    )


@pytest.mark.asyncio
async def test_stop_interrupts_timeout_without_requesting_restart():
    shutdown = asyncio.Event()
    service = TimeoutRestartService(
        Resource(value="resource"), timeout_minutes=60, shutdown_event=shutdown
    )
    service.write_resource_event = AsyncMock()
    task = service.start()
    await asyncio.wait_for(service.stop(), timeout=1)
    assert task.done()
    assert not shutdown.is_set()
    service.write_resource_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_elapsed_timeout_requests_shutdown_without_blocking_loop():
    shutdown = asyncio.Event()
    service = TimeoutRestartService(
        Resource(value="resource"), timeout_minutes=1, shutdown_event=shutdown
    )
    service.write_resource_event = AsyncMock()
    service._interval = 0.03
    loop = asyncio.get_running_loop()
    started = loop.time()
    task = service.start()
    await asyncio.wait_for(shutdown.wait(), timeout=1)
    await task
    assert loop.time() - started >= service._interval


@pytest.mark.asyncio
async def test_restarting_service_schedules_a_new_deadline():
    shutdown = asyncio.Event()
    service = TimeoutRestartService(
        Resource(value="resource"), timeout_minutes=1, shutdown_event=shutdown
    )
    service.write_resource_event = AsyncMock()
    service._interval = 0.01
    for expected_events in (1, 2):
        shutdown.clear()
        task = service.start()
        await asyncio.wait_for(shutdown.wait(), timeout=1)
        await task
        assert service.write_resource_event.await_count == expected_events


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_prevent_timeout_shutdown():
    shutdown = asyncio.Event()
    service = TimeoutRestartService(
        Resource(value="resource"), timeout_minutes=1, shutdown_event=shutdown
    )
    service.write_resource_event = AsyncMock(side_effect=RuntimeError("API unavailable"))
    with pytest.raises(RuntimeError, match="API unavailable"):
        await service.execute()
    assert shutdown.is_set()
    await service.execute()
    service.write_resource_event.assert_awaited_once()


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_timeout_rejects_nonpositive_values(timeout):
    with pytest.raises(ValueError, match="positive"):
        TimeoutRestartService(Resource(value="resource"), timeout_minutes=timeout)
