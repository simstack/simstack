"""Installation contention and lease fencing never kill unrelated runner work."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.services import workflow_installation_service as service_module
from simstack.runner_api.errors import RunnerApiError


def service():
    return service_module.WorkflowInstallationService("resource", 1)


@pytest.mark.asyncio
async def test_competing_installer_claim_is_normal_contention(monkeypatch):
    api = SimpleNamespace(
        pending_workflow_installations=AsyncMock(
            return_value=[{"installation_id": "job"}]
        ),
        claim_workflow_installation=AsyncMock(
            side_effect=RunnerApiError("claimed", status_code=409)
        ),
    )
    monkeypatch.setattr(service_module, "context", SimpleNamespace(runner_api=api))
    await service().execute()
    api.claim_workflow_installation.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_report_cannot_publish_or_stop_runner(monkeypatch):
    api = SimpleNamespace(
        report_workflow_installation=AsyncMock(
            side_effect=RunnerApiError("expired", status_code=409)
        )
    )
    monkeypatch.setattr(service_module, "context", SimpleNamespace(runner_api=api))
    await service()._report(
        "job", {"revision": "a" * 64, "claim_id": "claim", "attempt": 1}, "ready"
    )


@pytest.mark.asyncio
async def test_installation_renews_exact_lease_and_stops_after_preparation(monkeypatch):
    api = SimpleNamespace(
        claim_workflow_installation=AsyncMock(
            return_value={
                "workflow_revision": "a" * 64,
                "claim_id": "claim",
                "attempt": 1,
            }
        )
    )
    monkeypatch.setattr(service_module, "context", SimpleNamespace(runner_api=api))
    release = asyncio.Event()

    async def prepare():
        await release.wait()
        return "prepared"

    async def notify(*args, **kwargs):
        release.set()
        return {"workflow_revision": "a" * 64, "claim_id": "claim", "attempt": 1}

    api.claim_workflow_installation.side_effect = notify
    result = await asyncio.wait_for(
        service()._prepare_with_lease(
            "job",
            "original-request",
            0.03,
            {"revision": "a" * 64, "claim_id": "claim", "attempt": 1},
            prepare(),
        ),
        timeout=1,
    )
    assert result == "prepared"
    api.claim_workflow_installation.assert_awaited_once_with(
        "job", request_id="original-request", lease_seconds=0.03
    )
