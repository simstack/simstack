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
async def test_runner_cleanup_file_cache_sweeps_in_bounded_pages(
        initialized_context, monkeypatch, tmp_path
):
    """The file-cache sweep must page through FileStack documents.

    It used to load the whole FileStack collection (including embedded
    content blobs) into the runner on every cleanup cycle, which grows
    unboundedly on a shared database."""
    from unittest.mock import patch
    from simstack.models.files import FileStack
    from simstack.models.file_instance import FileInstance

    monkeypatch.setenv("SIMSTACK_FILE_CACHE_SWEEP_BATCH_SIZE", "2")
    monkeypatch.setenv("SIMSTACK_FILE_CACHE_SWEEP_MAX_PER_CYCLE", "100")

    resource = Resource(value="self")
    service = RunnerCleanupService(resource, interval=300)

    # The mongomock DB is shared across tests in this module; clear stale stacks.
    fs_collection = context.db.get_collection(FileStack)
    await fs_collection.delete_many({})

    old_ts = datetime.now() - timedelta(days=30)
    for i in range(3):
        f = tmp_path / f"cache_file_{i}.out"
        f.write_text(f"content {i}")
        stack = FileStack(
            name=f"stack_{i}",
            locations=[
                FileInstance(
                    path=str(f),
                    resource=resource,
                    created_at=old_ts,
                    location_type="local_path",
                    status="available",
                    is_cached=True,
                    last_accessed_at=old_ts,
                )
            ],
        )
        await context.db.save(stack)

    captured = []
    original_find = context.db.find

    async def spy_find(*args, **kwargs):
        model = args[0]
        captured.append(
            (model, kwargs.get("skip"), kwargs.get("limit"))
        )
        return await original_find(*args, **kwargs)

    monkeypatch.setattr(context.db, "find", spy_find)

    with patch("simstack.core.services.runner_cleanup_service.context") as mock_ctx:
        mock_ctx.config.workdir = tmp_path
        mock_ctx.config.project_root = tmp_path
        mock_ctx.db = context.db
        mock_ctx.initialized = True
        await service.execute()

    # All three expired cache files were actually deleted...
    for i in range(3):
        assert not (tmp_path / f"cache_file_{i}.out").exists()

    # ... but the sweep only ever materialized bounded pages.
    fs_calls = [c for c in captured if c[0] is FileStack]
    assert len(fs_calls) >= 2
    assert all(call[2] == 2 for call in fs_calls)  # limit == batch size
    assert [call[1] for call in fs_calls] == [0, 2]  # skip advances per page

    stacks = await context.db.find(FileStack)
    assert len(stacks) == 3
    assert all(stack.locations[0].status == "deleted" for stack in stacks)


@pytest.mark.asyncio
async def test_runner_cleanup_file_cache_budget_resumes_next_cycle(
        initialized_context, monkeypatch, tmp_path
):
    """When the per-cycle sweep budget is hit, the next cycle must resume
    from the saved offset instead of restarting at index 0."""
    from unittest.mock import patch
    from simstack.models.files import FileStack
    from simstack.models.file_instance import FileInstance

    monkeypatch.setenv("SIMSTACK_FILE_CACHE_SWEEP_BATCH_SIZE", "2")
    monkeypatch.setenv("SIMSTACK_FILE_CACHE_SWEEP_MAX_PER_CYCLE", "2")

    resource = Resource(value="self")
    service = RunnerCleanupService(resource, interval=300)

    # The mongomock DB is shared across tests in this module; clear stale stacks.
    fs_collection = context.db.get_collection(FileStack)
    await fs_collection.delete_many({})

    old_ts = datetime.now() - timedelta(days=30)
    for i in range(3):
        f = tmp_path / f"budget_file_{i}.out"
        f.write_text(f"content {i}")
        stack = FileStack(
            name=f"stack_{i}",
            locations=[
                FileInstance(
                    path=str(f),
                    resource=resource,
                    created_at=old_ts,
                    location_type="local_path",
                    status="available",
                    is_cached=True,
                    last_accessed_at=old_ts,
                )
            ],
        )
        await context.db.save(stack)

    async def _execute_cycle():
        with patch("simstack.core.services.runner_cleanup_service.context") as mock_ctx:
            mock_ctx.config.workdir = tmp_path
            mock_ctx.config.project_root = tmp_path
            mock_ctx.db = context.db
            mock_ctx.initialized = True
            await service.execute()

    # First cycle: budget (2) consumed by the first page only.
    # (Which exact files get cleaned depends on collection order, so only
    # the *count* of surviving files is asserted.)
    await _execute_cycle()
    survivors = list(tmp_path.glob("budget_file_*.out"))
    assert len(survivors) == 1
    assert service._sweep_offset == 2

    # Second cycle resumes at offset 2 and completes the sweep.
    await _execute_cycle()
    assert list(tmp_path.glob("budget_file_*.out")) == []
    assert service._sweep_offset == 0


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
