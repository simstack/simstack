import pytest

from simstack.core.context import context
from simstack.core.node import Node, node
from simstack.models import FloatData, NodeModel, NodeRegistry, Parameters


async def _implementation_before(data: FloatData, **kwargs) -> FloatData:
    return FloatData(value=data.value + 1)


async def _implementation_after(data: FloatData, **kwargs) -> FloatData:
    return FloatData(value=data.value + 2)


@pytest.mark.asyncio
async def test_changed_implementation_executes_instead_of_loading_old_results(initialized_context, monkeypatch):
    # Model one public node before and after a code update, with the same
    # module, qualified name and inputs so only its implementation differs.
    # Both revisions live in this interpreter; exercise the real execution and
    # cache persistence paths without starting a child that can import only one.
    monkeypatch.setattr(Node, "run_node_as_process", Node.execute_node_locally)
    for implementation in (_implementation_before, _implementation_after):
        implementation.__name__ = "versioned_workflow"
        implementation.__qualname__ = "versioned_workflow"
    old_node = node(_implementation_before)
    updated_node = node(_implementation_after)
    mapping = NodeModel(
        name="versioned_workflow",
        function_mapping=f"{__name__}._implementation_before",
        input_mappings=[], default_parameters=Parameters(),
    )
    await context.db.save(mapping)
    await context.refresh_mappings()
    inputs = FloatData(value=8102026.0)
    try:
        assert (await old_node(inputs)).value == 8102027.0
        assert (await old_node(inputs)).value == 8102027.0
        old_entries = await context.db.find(NodeRegistry, NodeRegistry.name == "versioned_workflow")
        assert len(old_entries) == 1

        assert (await updated_node(inputs)).value == 8102028.0
        entries = await context.db.find(NodeRegistry, NodeRegistry.name == "versioned_workflow")
        assert len(entries) == 2
        assert len({entry.function_hash for entry in entries}) == 2
    finally:
        entries = await context.db.find(NodeRegistry, NodeRegistry.name == "versioned_workflow")
        for entry in entries:
            await context.db.delete(entry)
        await context.db.delete(mapping)
        await context.db.delete(inputs)
        await context.refresh_mappings()
