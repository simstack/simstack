import asyncio
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from simstack.core.services.runner_manager import RunnerManager, runner_pid_file
from simstack.models.parameters import Resource


@pytest.fixture
def resource():
    return Resource(value="self")


@pytest.fixture
def runner_manager(resource):
    return RunnerManager(resource=resource, with_file_transfer=False)


@pytest.mark.asyncio
async def test_runner_manager_pid_file(runner_manager, tmp_path):
    runner_manager._pid_file = runner_pid_file(tmp_path, runner_manager._resource)

    with patch.object(runner_manager, "_is_process_running", return_value=False):
        runner_manager._check_existing_runner()
        runner_manager._write_pid_file()

    assert runner_manager._pid_file.exists()
    assert runner_manager._pid_file.read_text() == str(runner_manager._pid)
    assert stat.S_IMODE(runner_manager._pid_file.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_runner_manager_existing_runner_error(runner_manager, tmp_path):
    runner_manager._pid_file = runner_pid_file(tmp_path, runner_manager._resource)
    existing_pid = 12345
    runner_manager._pid_file.write_text(str(existing_pid))

    with (
        patch.object(runner_manager, "_is_process_running", return_value=True),
        pytest.raises(
            RuntimeError,
            match="Another runner for resource .* is already running",
        ),
    ):
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
    with (
        patch(
            "simstack.core.services.runner_manager.NodeExecutionService"
        ) as mock_node_svc_cls,
        patch(
            "simstack.core.services.runner_manager.RunnerStatusService"
        ) as mock_stat_svc_cls,
        patch(
            "simstack.core.services.runner_manager.SlurmStatusService"
        ) as mock_slurm_svc_cls,
        patch(
            "simstack.core.services.runner_manager.StopCheckService"
        ) as mock_stop_svc_cls,
    ):
        for svc_cls in (
            mock_node_svc_cls,
            mock_stat_svc_cls,
            mock_slurm_svc_cls,
            mock_stop_svc_cls,
        ):
            svc_cls.return_value.stop = AsyncMock()
            svc_cls.return_value.start = MagicMock(
                return_value=asyncio.create_task(asyncio.sleep(1))
            )

        runner_manager._pid_file = runner_pid_file(tmp_path, runner_manager._resource)

        async def set_shutdown():
            await asyncio.sleep(0.1)
            runner_manager._shutdown_event.set()

        asyncio.create_task(set_shutdown())
        await runner_manager.run_nodes_for_resource(polling_interval=0.1)

        assert runner_manager._pid_file.exists()
        mock_node_svc_cls.return_value.stop.assert_called()
