import asyncio
import time
import pytest
from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerEventEnum, RunnerType
from simstack.core.services.base_service import BaseService

class MockService(BaseService):
    def __init__(self, name, resource, interval):
        super().__init__(name, resource, interval)
        self.execute_count = 0
        self.execute_event = asyncio.Event()

    async def execute(self):
        self.execute_count += 1
        self.execute_event.set()
        self.execute_event.clear()


class FailingService(BaseService):
    def __init__(self, name, resource, interval):
        super().__init__(name, resource, interval)
        self.execute_count = 0

    async def execute(self):
        self.execute_count += 1
        raise RuntimeError("temporary failure")


@pytest.mark.asyncio
async def test_base_service_waits_between_failed_executions(monkeypatch):
    resource = Resource(value="self")
    service = FailingService("FailingService", resource, interval=60)
    wait_timeouts = []
    original_wait_for = asyncio.wait_for

    async def record_wait_for(awaitable, timeout):
        if service._stop_event.is_set():
            return await original_wait_for(awaitable, timeout)

        awaitable.close()
        wait_timeouts.append(timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", record_wait_for)

    await service.start()

    assert service.execute_count == 3
    assert len(wait_timeouts) == 2
    assert all(timeout > 0 for timeout in wait_timeouts)


@pytest.mark.asyncio
async def test_base_service_lifecycle(initialized_context):
    resource = Resource(value="self")
    interval = 1
    service = MockService("TestService", resource, interval)
    
    # Start the service
    task = service.start()
    assert task is not None
    assert not task.done()
    
    # Wait for at least one execution
    await asyncio.wait_for(service.execute_event.wait(), timeout=5)
    assert service.execute_count >= 1
    
    # Stop the service
    await service.stop()
    assert task.done()
    assert service._stop_event.is_set()

@pytest.mark.asyncio
async def test_base_service_write_resource_event(initialized_context):
    resource = Resource(value="self")
    service = MockService("TestService", resource, 60)
    
    await service.write_resource_event(RunnerEventEnum.ALIVE, message="Test Message")
    
    # Check if event was saved
    event = await context.db.find_one(
        RunnerEvent,
        (RunnerEvent.runner_type == RunnerType.RESOURCE_RUNNER)
        & (RunnerEvent.resource == resource)
        & (RunnerEvent.event == RunnerEventEnum.ALIVE)
    )
    assert event is not None
    # We don't check the exact uptime string as it can be 0 or 1s
    assert event.message.startswith("Uptime: ")

@pytest.mark.asyncio
async def test_base_service_runs_garbage_collection_each_poll(monkeypatch):
    """Every poll iteration must trigger a cheap gen-0/1 gc, plus a full
    sweep once the 30-minute timer expired (breaks runtime reference
    cycles so interpreter memory stays reusable)."""
    from simstack.core.services import base_service

    collected = []
    calls = {"n": 0}

    def fake_collect(generation=None):
        calls["n"] += 1
        collected.append(generation)
        return 0

    monkeypatch.setattr(base_service.gc, "collect", fake_collect)

    resource = Resource(value="self")
    service = MockService("GCTest", resource, interval=60)
    # Pretend the last full sweep was long ago -> first iteration must
    # additionally run the full (all-generation) collection.
    service._last_full_gc = time.monotonic() - 2000

    task = service.start()
    for _ in range(40):
        if service.execute_count >= 1:
            break
        await asyncio.sleep(0.05)
    await service.stop()
    assert task.done()

    assert service.execute_count >= 1
    assert calls["n"] >= 2
    assert 1 in collected          # per-poll gen 0+1 sweep ran
    assert None in collected       # full sweep ran
    assert service._last_full_gc >= time.monotonic() - 5


@pytest.mark.asyncio
async def test_base_service_write_node_event(initialized_context):
    from odmantic import ObjectId
    resource = Resource(value="self")
    service = MockService("TestService", resource, 60)
    node_id = ObjectId()
    
    await service.write_node_event(RunnerEventEnum.NODE_STARTED, node_id, message="Node Started")
    
    # Check if event was saved
    event = await context.db.find_one(
        RunnerEvent,
        (RunnerEvent.runner_type == RunnerType.NODE_RUNNER)
        & (RunnerEvent.node_id == node_id)
        & (RunnerEvent.event == RunnerEventEnum.NODE_STARTED)
    )
    assert event is not None
    assert event.message == "Node Started"
