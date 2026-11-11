import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.services.runner_control_service import RunnerControlService


def control(monkeypatch, json):
    manager = SimpleNamespace(_resource="machine-a", _claiming_paused=asyncio.Event())
    from simstack.server_api import ServerAPI
    server = ServerAPI(SimpleNamespace(json=json))
    monkeypatch.setattr("simstack.core.services.runner_control_service.context", SimpleNamespace(server=server))
    service = RunnerControlService(manager)
    monkeypatch.setattr(service, "refresh_container_credentials", AsyncMock())
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("busy,status", [(True, "pending"), (False, "deferred_busy")])
async def test_busy_resource_defers_preparation_without_ack_or_pausing_old_tasks(monkeypatch, busy, status):
    command = {"id": "command", "action": "prepare_repository", "status": status, "payload": {}}
    api = AsyncMock(return_value={"busy": busy, "commands": [command]})
    prepare = AsyncMock()
    monkeypatch.setattr("simstack.util.repository_runtime.prepare_repository", prepare)
    service = control(monkeypatch, api)
    await service.execute()
    prepare.assert_not_awaited()
    assert api.await_count == 1
    assert not service.manager._claiming_paused.is_set()


@pytest.mark.asyncio
async def test_control_acknowledges_ready_only_after_successful_preparation(monkeypatch):
    descriptor = {"repository_revision": "a" * 64}
    command = {"id": "command", "action": "prepare_repository", "status": "pending", "payload": descriptor}
    calls = []
    async def json(method, path, body=None):
        calls.append((path, body))
        if method == "GET":
            return {"busy": False, "commands": [command]}
        if path.endswith("/status"):
            return {"status": "preparing"}
        assert calls[-2] == ("prepared", descriptor)
        return {"acknowledged": True}
    async def prepare(api, payload, *, repair_runtime):
        assert repair_runtime is True
        assert payload is descriptor
        calls.append(("prepared", payload))
    monkeypatch.setattr("simstack.util.repository_runtime.prepare_repository", prepare)
    service = control(monkeypatch, json)
    await service.execute()
    assert calls[-1] == ("/runner/control/command/ack", {"status": "ready"})
    assert not service.manager._claiming_paused.is_set()


@pytest.mark.asyncio
async def test_authoritative_server_busy_check_wins_race_before_git(monkeypatch):
    command = {"id": "command", "action": "prepare_repository", "payload": {}}
    api = AsyncMock(side_effect=[{"busy": False, "commands": [command]}, {"status": "deferred_busy"}])
    prepare = AsyncMock()
    monkeypatch.setattr("simstack.util.repository_runtime.prepare_repository", prepare)
    await control(monkeypatch, api).execute()
    prepare.assert_not_awaited()
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_preparation_failure_is_reported_without_secret_output_and_can_retry(monkeypatch):
    command = {"id": "command", "action": "prepare_repository", "payload": {}}
    api = AsyncMock(side_effect=[{"commands": [command]}, {"status": "preparing"}, {}])
    prepare = AsyncMock(side_effect=RuntimeError("private-key-bytes and token from helper"))
    monkeypatch.setattr("simstack.util.repository_runtime.prepare_repository", prepare)
    await control(monkeypatch, api).execute()
    body = api.await_args_list[-1].args[2]
    assert body["status"] == "failed"
    assert "private-key-bytes" not in body["error"]
    assert "token from helper" not in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,version", [("revoked", "1"), ("active", "2")])
async def test_revoked_or_rotated_key_requires_reinstall_before_new_preparation(monkeypatch, status, version):
    monkeypatch.setenv("SIMSTACK_GIT_SSH_KEY_VERSION", "1")
    command = {"id": "command", "action": "prepare_repository", "payload": {}}
    api = AsyncMock(side_effect=[{"commands": [command], "ssh_key_status": status, "ssh_key_version": version},
                                {"status": "preparing"}, {}])
    prepare = AsyncMock()
    monkeypatch.setattr("simstack.util.repository_runtime.prepare_repository", prepare)
    await control(monkeypatch, api).execute()
    prepare.assert_not_awaited()
    body = api.await_args_list[-1].args[2]
    assert body["status"] == "failed"
    assert "installer" in body["error"]
