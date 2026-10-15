"""Repository-owned artifact operations preserve every supported domain type."""

from unittest.mock import AsyncMock

import pytest

from simstack.core.artifacts import ArtifactArguments, create_artifacts, find_all_artifacts
from simstack.core.recompute_artifacts import recompute_artifacts
from simstack.models.artifact_models import ArtifactMapping, ArtifactModel
from simstack.models.charts_artifact import ChartArtifactModel
from simstack.models.images2d import Image2DArtifactModel
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters
from simstack.models.table_artifact import TableArtifactModel
from simstack.runner_protocol import RunnerTaskStatus
from tests.support.in_memory_repository import InMemoryRepository


def registry():
    return NodeRegistry(
        name="artifacts", status=RunnerTaskStatus.COMPLETED, call_path=".artifacts",
        function_hash="code", arg_hash="args", func_mapping="example.node",
        parameters=Parameters(),
    )


@pytest.mark.asyncio
async def test_all_typed_artifacts_are_tracked_loaded_and_async_mappings_awaited(monkeypatch):
    repository = InMemoryRepository()
    node = await repository.save(registry())
    artifacts = [ArtifactModel(name="generic"), ChartArtifactModel(title={"text": "chart"}), TableArtifactModel(), Image2DArtifactModel(name="image", format="png", data=b"pixels")]
    await repository.save(ArtifactMapping(name="first", regex_pattern=".*", function_mapping="example.first"))
    await repository.save(ArtifactMapping(name="second", regex_pattern=".*", function_mapping="example.second"))
    first, second = AsyncMock(return_value=artifacts[:2]), AsyncMock(return_value=artifacts[2:])
    importer = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr("simstack.core.artifacts.import_function", importer)
    node.artifact_ids = await create_artifacts(ArtifactArguments(None, node.id), node, repository)
    assert node.artifact_ids == [artifact.id for artifact in artifacts]
    loaded = await find_all_artifacts(node, repository)
    assert [type(artifact) for artifact in loaded] == [type(artifact) for artifact in artifacts]
    assert all(artifact.parent_id == node.id for artifact in loaded)
    first.assert_awaited_once()
    second.assert_awaited_once()


@pytest.mark.asyncio
async def test_recompute_deletes_only_owned_typed_artifacts(monkeypatch):
    repository = InMemoryRepository()
    node = await repository.save(registry())
    child_artifact = await repository.save(ArtifactModel(name="child"))
    owned = [ArtifactModel(name="generic"), ChartArtifactModel(title={"text": "chart"}), TableArtifactModel(), Image2DArtifactModel(name="image", format="png", data=b"pixels")]
    for artifact in owned:
        artifact.parent_id = node.id
        await repository.save(artifact)
    node.artifact_ids = [child_artifact.id, *(artifact.id for artifact in owned)]
    await repository.save(node)
    executor = AsyncMock()
    executor.status = RunnerTaskStatus.COMPLETED
    executor.name = node.name
    executor.load_results.return_value = None
    monkeypatch.setattr("simstack.core.recompute_artifacts.reconstruct_persisted_node", AsyncMock(return_value=executor))
    await recompute_artifacts(node, repository)
    assert not node.artifact_ids
    assert await repository.find(ArtifactModel) == [child_artifact]
    for model_class in (ChartArtifactModel, TableArtifactModel, Image2DArtifactModel):
        assert await repository.find(model_class) == []
