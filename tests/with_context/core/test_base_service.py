import asyncio
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
