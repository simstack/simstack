import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.core.services.base_service import BaseService
from simstack.runner_protocol import RunnerLogBatchRequestV1


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


class CountingService(BaseService):
    def __init__(self, name, resource, interval):
        super().__init__(name, resource, interval)
        self.execute_count = 0
        self.first_execution = asyncio.Event()

    async def execute(self):
        self.execute_count += 1
        self.first_execution.set()


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
async def test_one_second_interval_does_not_truncate_to_busy_loop():
    service = CountingService(
        "CountingService",
        Resource(value="self"),
        interval=1,
    )
    task = service.start()

    await asyncio.wait_for(service.first_execution.wait(), timeout=1)
    await asyncio.sleep(0.1)

    assert service.execute_count == 1
    await asyncio.wait_for(service.stop(), timeout=0.2)
    assert task.done()


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
async def test_base_service_write_resource_event(monkeypatch):
    resource = Resource(value="self")
    service = MockService("TestService", resource, 60)
    runner_api = SimpleNamespace(heartbeat=AsyncMock())
    monkeypatch.setattr(
        "simstack.core.services.base_service.context",
        SimpleNamespace(runner_api=runner_api),
    )

    await service.write_resource_event(RunnerEventEnum.ALIVE, message="Test Message")

    runner_api.heartbeat.assert_awaited_once()
    assert runner_api.heartbeat.await_args.kwargs["message"].startswith("Uptime: ")


@pytest.mark.asyncio
async def test_base_service_write_node_event():
    from simstack.models.document import ObjectId

    resource = Resource(value="self")
    service = MockService("TestService", resource, 60)
    node_id = ObjectId()
    execution_api = SimpleNamespace(send_logs=AsyncMock(return_value={}))

    await service.write_node_event(
        RunnerEventEnum.NODE_STARTED,
        node_id,
        execution_api=execution_api,
        message="Node Started",
    )

    execution_api.send_logs.assert_awaited_once()
    event = execution_api.send_logs.await_args.kwargs["events"][0]
    assert event["node_id"] == str(node_id)
    assert event["message"] == "Node Started"


@pytest.mark.asyncio
async def test_base_service_events_match_exact_runner_http_dto(monkeypatch):
    from simstack.models.document import ObjectId

    resource = Resource(value="self")
    service = MockService("TestService", resource, 60)
    supervisor_api = SimpleNamespace(send_logs=AsyncMock(return_value={}))
    execution_api = SimpleNamespace(send_logs=AsyncMock(return_value={}))
    monkeypatch.setattr(
        "simstack.core.services.base_service.context",
        SimpleNamespace(runner_api=supervisor_api),
    )

    await service.write_resource_event(RunnerEventEnum.RUNNER_STARTED)
    await service.write_node_event(
        RunnerEventEnum.NODE_STARTED,
        ObjectId(),
        execution_api=execution_api,
    )

    emitted_batches = (
        supervisor_api.send_logs.await_args.kwargs["events"],
        execution_api.send_logs.await_args.kwargs["events"],
    )
    for events in emitted_batches:
        request_json = json.dumps({"logs": [], "events": events})
        parsed = RunnerLogBatchRequestV1.model_validate_json(
            request_json,
            strict=True,
        )
        assert parsed.events[0].timestamp.endswith("+00:00")
