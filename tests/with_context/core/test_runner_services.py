import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from simstack.core.services.runner_status_service import RunnerStatusService
from simstack.core.services.stop_check_service import StopCheckService
from simstack.core.services.timeout_restart_service import TimeoutRestartService
from simstack.models.parameters import Resource


def _api_context(tmp_path, runner_api):
    return SimpleNamespace(
        runner_api=runner_api,
        config=SimpleNamespace(workdir=tmp_path, project_root=tmp_path),
    )


@pytest.mark.asyncio
async def test_runner_status_service(monkeypatch, tmp_path):
    runner_api = SimpleNamespace(heartbeat=AsyncMock(return_value={}))
    fake_context = _api_context(tmp_path, runner_api)
    monkeypatch.setattr("simstack.core.services.base_service.context", fake_context)
    service = RunnerStatusService(Resource(value="self"), interval=60)

    await service.execute()

    runner_api.heartbeat.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_check_service(monkeypatch, tmp_path):
    runner_api = SimpleNamespace(send_logs=AsyncMock(return_value={}))
    fake_context = _api_context(tmp_path, runner_api)
    monkeypatch.setattr("simstack.core.services.stop_check_service.context", fake_context)
    monkeypatch.setattr("simstack.core.services.base_service.context", fake_context)
    shutdown_event = asyncio.Event()
    service = StopCheckService(
        Resource(value="self"), interval=10, shutdown_event=shutdown_event
    )

    await service.execute()
    assert not shutdown_event.is_set()

    (tmp_path / "STOP").touch()
    await service.execute()

    assert shutdown_event.is_set()
    event = runner_api.send_logs.await_args.kwargs["events"][0]
    assert event["event"] == "shutdown"
    assert event["message"] == "STOP file found"


@pytest.mark.asyncio
async def test_timeout_restart_service(monkeypatch, tmp_path):
    runner_api = SimpleNamespace(send_logs=AsyncMock(return_value={}))
    fake_context = _api_context(tmp_path, runner_api)
    monkeypatch.setattr("simstack.core.services.base_service.context", fake_context)
    service = TimeoutRestartService(Resource(value="self"), timeout_minutes=1)

    with patch.object(service, "trigger_restart", AsyncMock()) as mock_restart:
        await service.execute()
        assert service._executed is True
        mock_restart.assert_awaited_once()
        event = runner_api.send_logs.await_args.kwargs["events"][0]
        assert "Timeout restart" in event["message"]

        mock_restart.reset_mock()
        await service.execute()
        mock_restart.assert_not_awaited()
