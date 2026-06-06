import asyncio
import pytest
import os
import platform
from unittest.mock import MagicMock, patch, PropertyMock, AsyncMock
from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.core.services.runner_manager import RunnerManager

@pytest.fixture
def resource():
    return Resource(value="self")

@pytest.fixture
def runner_manager(resource):
    return RunnerManager(resource=resource, detach=False)

@pytest.mark.asyncio
async def test_runner_manager_pid_file(runner_manager, tmp_path):
    with patch("simstack.core.services.runner_manager.context") as mock_context:
        mock_context.config.workdir = tmp_path
        runner_manager._pid_file = tmp_path / f"runner_{runner_manager._resource}.pid"
        
        # Check no existing runner (mock _is_process_running to False)
        with patch.object(runner_manager, "_is_process_running", return_value=False):
            runner_manager._check_existing_runner()
            
            # Now simulate running (write PID)
            runner_manager._pid_file.write_text(str(runner_manager._pid))
            assert runner_manager._pid_file.exists()
            assert runner_manager._pid_file.read_text() == str(runner_manager._pid)

@pytest.mark.asyncio
async def test_runner_manager_existing_runner_error(runner_manager, tmp_path):
    with patch("simstack.core.services.runner_manager.context") as mock_context:
        mock_context.config.workdir = tmp_path
        runner_manager._pid_file = tmp_path / f"runner_{runner_manager._resource}.pid"
        
        # Create a "fake" existing runner PID
        existing_pid = 12345
        runner_manager._pid_file.write_text(str(existing_pid))
        
        # Mock _is_process_running to return True for existing_pid
        with patch.object(runner_manager, "_is_process_running", return_value=True):
            with pytest.raises(RuntimeError, match="Another runner for resource .* is already running"):
                runner_manager._check_existing_runner()

@pytest.mark.asyncio
async def test_runner_manager_stop_services(runner_manager):
    mock_service1 = MagicMock()
    mock_service1.stop = AsyncMock()
    mock_service2 = MagicMock()
    mock_service2.stop = AsyncMock()
    
    runner_manager._services = [mock_service1, mock_service2]
    
    await runner_manager.stop_all_services()
    
    mock_service1.stop.assert_called_once()
    mock_service2.stop.assert_called_once()

@pytest.mark.asyncio
async def test_runner_manager_run_nodes_orchestration(runner_manager, tmp_path):
    # This is a complex test as it starts multiple services. 
    # We'll mock the services and their start methods.
    
    with patch("simstack.core.services.runner_manager.context") as mock_context, \
         patch("simstack.core.services.runner_manager.NodeExecutionService") as mock_node_svc_cls, \
         patch("simstack.core.services.runner_manager.RunnerStatusService") as mock_stat_svc_cls, \
         patch("simstack.core.services.runner_manager.RunnerCleanupService") as mock_clean_svc_cls, \
         patch("simstack.core.services.runner_manager.SlurmStatusService") as mock_slurm_svc_cls, \
         patch("simstack.core.services.runner_manager.StopCheckService") as mock_stop_svc_cls, \
         patch("simstack.core.services.runner_manager.GitUvUpdateService") as mock_git_svc_cls, \
         patch("simstack.core.services.runner_manager.ResourceBranchMonitorService") as mock_branch_svc_cls:
        
        # Ensure all mock service instances have an AsyncMock stop method
        for svc_cls in [mock_node_svc_cls, mock_stat_svc_cls, mock_clean_svc_cls, 
                        mock_slurm_svc_cls, mock_stop_svc_cls, mock_git_svc_cls, mock_branch_svc_cls]:
            svc_cls.return_value.stop = AsyncMock()
            svc_cls.return_value.start = MagicMock(return_value=asyncio.create_task(asyncio.sleep(0.01)))

        mock_context.config.workdir = tmp_path
        runner_manager._pid_file = tmp_path / f"runner_{runner_manager._resource}.pid"
        
        # Setup shutdown event to be set
        async def set_shutdown():
            await asyncio.sleep(0.1)
            runner_manager._shutdown_event.set()
        
        asyncio.create_task(set_shutdown())
        
        # Run orchestration
        await runner_manager.run_nodes_for_resource(polling_interval=0.1)
        
        assert runner_manager._pid_file.exists()
        # Ensure stop was called on services
        mock_node_svc_cls.return_value.stop.assert_called()
