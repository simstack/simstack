import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from odmantic import ObjectId

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
    deleted = []
    async def delete(item):
        deleted.append(item.id)

    async def load_results():
        assert deleted == []
        return SimpleNamespace(energy_chart=outputs[1])

    db = SimpleNamespace(
        find=AsyncMock(side_effect=[[], *[
            [output, artifact] for output, artifact in zip(outputs, generated)
        ]]),
        delete=AsyncMock(side_effect=delete), find_one=AsyncMock(return_value=None),
        save=AsyncMock(),
    )
    node = SimpleNamespace(status=TaskStatus.COMPLETED, load_results=AsyncMock(side_effect=load_results))
    monkeypatch.setattr(module, "context", SimpleNamespace(db=db))
    monkeypatch.setattr(module, "node_from_database", AsyncMock(return_value=node))
    monkeypatch.setattr(module, "import_function", AsyncMock(return_value=lambda: None))
    create = AsyncMock(return_value=[])
    monkeypatch.setattr(module, "create_artifacts", create)

    await module.recompute_artifacts(registry)
    assert deleted == [item.id for item in generated]
    assert not set(deleted) & {item.id for item in outputs}
    node.load_results.assert_awaited_once()
    create.assert_awaited_once()
    db.save.assert_awaited_once_with(registry)


@pytest.mark.asyncio
async def test_missing_output_does_not_delete_existing_artifacts(monkeypatch, recompute_module):
    module = recompute_module
    db = SimpleNamespace(find=AsyncMock(return_value=[]), delete=AsyncMock())
    registry = SimpleNamespace(id=ObjectId(), status=TaskStatus.COMPLETED)
    node = SimpleNamespace(
        status=TaskStatus.COMPLETED,
        load_results=AsyncMock(side_effect=ValueError("missing output")),
    )
    monkeypatch.setattr(module, "context", SimpleNamespace(db=db))
    monkeypatch.setattr(module, "node_from_database", AsyncMock(return_value=node))
    with pytest.raises(ValueError, match="missing output"):
        await module.recompute_artifacts(registry)
    db.delete.assert_not_awaited()
