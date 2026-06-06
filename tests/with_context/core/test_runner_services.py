import asyncio
import pytest
import os
from datetime import datetime, timedelta
from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerEventEnum, RunnerType
from simstack.core.services.runner_status_service import RunnerStatusService
from simstack.core.services.stop_check_service import StopCheckService
from simstack.core.services.runner_cleanup_service import RunnerCleanupService
from simstack.core.services.timeout_restart_service import TimeoutRestartService

@pytest.mark.asyncio
async def test_runner_status_service(initialized_context):
    resource = Resource(value="self")
    service = RunnerStatusService(resource, interval=60)
    
    await service.execute()
    
    event = await context.db.find_one(
        RunnerEvent,
        (RunnerEvent.resource == resource) & (RunnerEvent.event == RunnerEventEnum.ALIVE)
    )
    assert event is not None

@pytest.mark.asyncio
async def test_stop_check_service(initialized_context, tmp_path):
    from unittest.mock import patch, PropertyMock, MagicMock
    resource = Resource(value="self")
    shutdown_event = asyncio.Event()
    
    # Patch context.config to mock workdir
    mock_config = MagicMock()
    type(mock_config).workdir = PropertyMock(return_value=tmp_path)
    
    # Use patch and manually restore if needed, or patch a sub-attribute if possible
    # Since patching 'config' property fails on exit, let's patch where it's used if possible
    # or just mock the file system check in StopCheckService.
    
    with patch("simstack.core.services.stop_check_service.context") as mock_context:
        mock_context.config.workdir = tmp_path
        service = StopCheckService(resource, interval=10, shutdown_event=shutdown_event)
        
        # Initially no STOP file
        await service.execute()
        assert not shutdown_event.is_set()
        
        # Create STOP file
        stop_file = tmp_path / "STOP"
        stop_file.touch()
        
        await service.execute()
        assert shutdown_event.is_set()
        
        # Check event
        event = await context.db.find_one(
            RunnerEvent,
            (RunnerEvent.resource == resource) & (RunnerEvent.event == RunnerEventEnum.SHUTDOWN)
        )
        assert event is not None
        assert event.message == "STOP file found"

import pytest_asyncio

@pytest_asyncio.fixture(autouse=True)
async def clear_events():
    collection = context.db.get_collection(RunnerEvent)
    await collection.delete_many({})
    yield

@pytest.mark.asyncio
async def test_runner_cleanup_service(initialized_context):
    resource = Resource(value="self")
    service = RunnerCleanupService(resource, interval=300)
    
    # Create an old event
    old_time = datetime.now() - timedelta(minutes=35)
    old_event = RunnerEvent(
        resource=resource,
        runner_type=RunnerType.RESOURCE_RUNNER,
        event=RunnerEventEnum.ALIVE,
        timestamp=old_time
    )
    await context.db.save(old_event)
    
    # Create a recent event
    recent_event = RunnerEvent(
        resource=resource,
        runner_type=RunnerType.RESOURCE_RUNNER,
        event=RunnerEventEnum.ALIVE,
        timestamp=datetime.now()
    )
    await context.db.save(recent_event)
    
    await service.execute()
    
    # Old event should be gone
    found_old = await context.db.find_one(RunnerEvent, RunnerEvent.id == old_event.id)
    assert found_old is None
    
    # Recent event should still be there
    found_recent = await context.db.find_one(RunnerEvent, RunnerEvent.id == recent_event.id)
    assert found_recent is not None

@pytest.mark.asyncio
async def test_timeout_restart_service(initialized_context):
    from unittest.mock import patch, AsyncMock
    resource = Resource(value="self")
    # Using small timeout for testing
    service = TimeoutRestartService(resource, timeout_minutes=1)
    
    with patch.object(service, "trigger_restart", AsyncMock()) as mock_restart:
        await service.execute()
        assert service._executed is True
        mock_restart.assert_called_once()
        
        # Check event
        event = await context.db.find_one(
            RunnerEvent,
            (RunnerEvent.resource == resource) & (RunnerEvent.event == RunnerEventEnum.SHUTDOWN)
        )
        assert event is not None
        assert "Timeout restart" in event.message
        
        # Second execution should do nothing
        mock_restart.reset_mock()
        await service.execute()
        mock_restart.assert_not_called()
