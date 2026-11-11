from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import simstack.core.artifacts as artifacts
from simstack.server_api.http_transport import RunnerAPIError
from simstack.server_api.scientific_values import ScientificValuesAPI


@pytest.mark.asyncio
async def test_artifact_api_failure_is_not_converted_to_empty_replacement(monkeypatch):
    failure = RunnerAPIError("Server unavailable")
    monkeypatch.setattr(artifacts, "context", SimpleNamespace(server=SimpleNamespace(values=object())))
    monkeypatch.setattr(artifacts, "find_artifact_mappings", AsyncMock(side_effect=failure))
    registry = SimpleNamespace(id="task-a", call_path="parent.child", name="child")
    with pytest.raises(RunnerAPIError, match="Server unavailable"):
        await artifacts.create_artifacts(artifacts.ArtifactArguments(None), registry)


@pytest.mark.asyncio
async def test_legacy_task_without_call_path_has_no_path_mapping():
    requests = SimpleNamespace(read=AsyncMock())
    assert await ScientificValuesAPI(requests).artifact_mappings(None) == []
    requests.read.assert_not_awaited()
