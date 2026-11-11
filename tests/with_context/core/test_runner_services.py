from server import storage_criteria
import asyncio
import pytest
import os
from datetime import datetime, timedelta, timezone
from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerEventEnum, RunnerType
from simstack.core.services.runner_status_service import RunnerStatusService
from simstack.core.services.stop_check_service import StopCheckService
from simstack.core.services.runner_cleanup_service import RunnerCleanupService
from simstack.core.services.timeout_restart_service import TimeoutRestartService

@pytest.mark.asyncio
async def test_runner_status_service(initialized_context, server_storage):
    resource = Resource(value="test")
    service = RunnerStatusService(resource, interval=60)
    
    await service.execute()
    
    event = await server_storage.find_one(
        RunnerEvent,
        (storage_criteria.field(RunnerEvent, 'resource') == resource) & (storage_criteria.field(RunnerEvent, 'event') == RunnerEventEnum.ALIVE)
    )
    assert event is not None

@pytest.mark.asyncio
async def test_stop_check_service(initialized_context, tmp_path, server_storage):
    from unittest.mock import patch, PropertyMock, MagicMock
    resource = Resource(value="test")
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
        event = await server_storage.find_one(
            RunnerEvent,
            (storage_criteria.field(RunnerEvent, 'resource') == resource) & (storage_criteria.field(RunnerEvent, 'event') == RunnerEventEnum.SHUTDOWN)
        )
        assert event is not None
        assert event.message == "STOP file found"

import pytest_asyncio

@pytest_asyncio.fixture(autouse=True)
async def clear_events(server_storage):
    collection = server_storage.get_collection(RunnerEvent)
    await collection.delete_many({})
    yield

@pytest.mark.asyncio
async def test_runner_cleanup_service(initialized_context, server_storage):
    resource = Resource(value="test")
    service = RunnerCleanupService(resource, interval=300)
    
    # Create an old event
    old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
    old_event = RunnerEvent(
        resource=resource,
        runner_type=RunnerType.RESOURCE_RUNNER,
        event=RunnerEventEnum.RUNNER_STARTED,
        timestamp=old_time
    )
    await server_storage.save(old_event)
    
    # Create a recent event
    recent_event = RunnerEvent(
        resource=resource,
        runner_type=RunnerType.RESOURCE_RUNNER,
        event=RunnerEventEnum.ALIVE,
        timestamp=datetime.now(timezone.utc)
    )
    await server_storage.save(recent_event)
    
    await RunnerStatusService(resource, interval=60).execute()
    
    # Server applies retention when receiving the resource heartbeat.
    found_old = await server_storage.find_one(RunnerEvent, storage_criteria.field(RunnerEvent, 'id') == old_event.id)
    assert found_old is None
    
    # Recent event should still be there
    found_recent = await server_storage.find_one(RunnerEvent, storage_criteria.field(RunnerEvent, 'id') == recent_event.id)
    assert found_recent is not None

@pytest.mark.asyncio
async def test_timeout_restart_service(initialized_context, server_storage):
    from unittest.mock import patch, AsyncMock
    resource = Resource(value="test")
    # Using small timeout for testing
    service = TimeoutRestartService(resource, timeout_minutes=1)
    
    with patch.object(service, "trigger_restart", AsyncMock()) as mock_restart:
        await service.execute()
        assert service._executed is True
        mock_restart.assert_called_once()
        
        # Check event
        event = await server_storage.find_one(
            RunnerEvent,
            (storage_criteria.field(RunnerEvent, 'resource') == resource) & (storage_criteria.field(RunnerEvent, 'event') == RunnerEventEnum.SHUTDOWN)
        )
        assert event is not None
        assert "Timeout restart" in event.message
        
        # Second execution should do nothing
        mock_restart.reset_mock()
        await service.execute()
        mock_restart.assert_not_called()
