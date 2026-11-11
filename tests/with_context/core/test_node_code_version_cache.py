from server import storage_criteria
import pytest

from simstack.core.context import context
from simstack.core.node import Node, node
from simstack.models import FloatData, NodeModel, NodeRegistry, Parameters


async def _implementation_before(data: FloatData, **kwargs) -> FloatData:
    return FloatData(value=data.value + 1)


async def _implementation_after(data: FloatData, **kwargs) -> FloatData:
    return FloatData(value=data.value + 2)


@pytest.mark.asyncio
async def test_changed_implementation_reuses_cached_results(initialized_context, server_storage, monkeypatch):
    # Model one public node before and after a code update, with the same
    # module, qualified name and inputs. Cache identity is name + arg_hash.
    for implementation in (_implementation_before, _implementation_after):
        monkeypatch.setattr(implementation, "__name__", "versioned_workflow")
        monkeypatch.setattr(implementation, "__qualname__", "versioned_workflow")
    old_node = node(_implementation_before)
    updated_node = node(_implementation_after)
    mapping = NodeModel(
        name="versioned_workflow",
        function_mapping=f"{__name__}._implementation_before",
        input_mappings=[], default_parameters=Parameters(),
    )
    await server_storage.save(mapping)
    await context.refresh_mappings()
    inputs = FloatData(value=8102026.0)
    try:
        assert (await old_node(inputs)).value == 8102027.0
        assert (await old_node(inputs)).value == 8102027.0
        old_entries = await server_storage.find(NodeRegistry, storage_criteria.field(NodeRegistry, 'name') == "versioned_workflow")
        assert len(old_entries) == 1

        assert (await updated_node(inputs)).value == 8102027.0
        entries = await server_storage.find(NodeRegistry, storage_criteria.field(NodeRegistry, 'name') == "versioned_workflow")
        assert len(entries) == 1
        assert entries[0].function_hash == ""
    finally:
        entries = await server_storage.find(NodeRegistry, storage_criteria.field(NodeRegistry, 'name') == "versioned_workflow")
        for entry in entries:
            await server_storage.delete(entry)
        await server_storage.delete(mapping)
        await server_storage.delete(inputs)
        await context.refresh_mappings()
