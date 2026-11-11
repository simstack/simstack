import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from simstack.models.document import ObjectId

from simstack.core.definitions import TaskStatus


@pytest.fixture
def recompute_module():
    return importlib.import_module("simstack.core.recompute_artifacts")


@pytest.mark.asyncio
async def test_recompute_preserves_outputs_and_removes_generated_artifacts(
    monkeypatch, recompute_module
):
    module = recompute_module
    outputs = [SimpleNamespace(id=ObjectId()) for _ in range(4)]
    generated = [SimpleNamespace(id=ObjectId()) for _ in range(4)]
    registry = SimpleNamespace(
        id=ObjectId(), status=TaskStatus.COMPLETED, name="run_ga_conformer_gen",
        results_references=[SimpleNamespace(reference=item.id) for item in outputs],
        artifact_ids=[outputs[3].id, generated[3].id], input_references=[],
        func_mapping="test.ga", call_path="parent.ga", parameters=None,
    )
    api = SimpleNamespace(children=AsyncMock(return_value=[]), resolve_values=AsyncMock(return_value=[]),
                          clear_generated_artifacts=AsyncMock(), replace_artifacts=AsyncMock())
    node = SimpleNamespace(status=TaskStatus.COMPLETED, _args=[],
        load_results=AsyncMock(return_value=SimpleNamespace(energy_chart=outputs[1])))
    monkeypatch.setattr(module, "context", SimpleNamespace(server=SimpleNamespace(values=api, tasks=api)))
    monkeypatch.setattr(module, "node_from_task", AsyncMock(return_value=node))
    monkeypatch.setattr(module, "import_function", AsyncMock(return_value=lambda: None))
    create = AsyncMock(return_value=[generated[0].id])
    monkeypatch.setattr(module, "create_artifacts", create)

    await module.recompute_artifacts(registry)
    node.load_results.assert_awaited_once()
    create.assert_awaited_once()
    api.clear_generated_artifacts.assert_awaited_once_with(registry)
    api.replace_artifacts.assert_awaited_once_with(registry, [generated[0].id])
    assert registry.results_references == [SimpleNamespace(reference=item.id) for item in outputs]


@pytest.mark.asyncio
async def test_missing_output_does_not_delete_existing_artifacts(monkeypatch, recompute_module):
    module = recompute_module
    api = SimpleNamespace(children=AsyncMock(return_value=[]), clear_generated_artifacts=AsyncMock(), replace_artifacts=AsyncMock())
    registry = SimpleNamespace(id=ObjectId(), status=TaskStatus.COMPLETED)
    node = SimpleNamespace(
        status=TaskStatus.COMPLETED,
        load_results=AsyncMock(side_effect=ValueError("missing output")),
    )
    monkeypatch.setattr(module, "context", SimpleNamespace(server=SimpleNamespace(values=api, tasks=api)))
    monkeypatch.setattr(module, "node_from_task", AsyncMock(return_value=node))
    with pytest.raises(ValueError, match="missing output"):
        await module.recompute_artifacts(registry)
    api.clear_generated_artifacts.assert_not_awaited()
    api.replace_artifacts.assert_not_awaited()
