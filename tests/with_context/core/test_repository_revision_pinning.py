import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import Node, _find_reusable_task
from simstack.models import FloatData, NodeModel, NodeRegistry, Parameters


def registry(revision, **values):
    return NodeRegistry(name="pinned_cache", status=TaskStatus.COMPLETED,
        func_mapping="workflow.pinned_cache", function_hash="", arg_hash="same-arguments",
        parameters=Parameters(), repository_revision=revision, **values)


@pytest.mark.asyncio
async def test_completed_results_are_reused_only_within_the_same_repository_revision(initialized_context):
    first = registry("a"*64, repository_version=1)
    second = registry("b"*64, repository_version=2)
    await context.db.save(first)
    await context.db.save(second)
    try:
        for expected in (first,second):
            found = await _find_reusable_task(context.db,name="pinned_cache",arg_hash="same-arguments",
                execution_parameters=Parameters(),repository_revision=expected.repository_revision)
            assert found.id == expected.id
        assert await _find_reusable_task(context.db,name="pinned_cache",arg_hash="same-arguments",
            execution_parameters=Parameters(),repository_revision="c"*64) is None
    finally:
        await context.db.delete(first)
        await context.db.delete(second)


async def child_function(value: FloatData) -> FloatData:
    return value


@pytest.mark.asyncio
async def test_nested_node_inherits_parent_pin_when_latest_catalogue_has_advanced(initialized_context):
    mapping = NodeModel(name="child_function", function_mapping=f"{__name__}.child_function",
        input_mappings=[], default_parameters=Parameters(),repository_revision="b"*64, repository_version=2)
    parent = registry("a"*64,repository_version=1)
    await context.db.save(parent)
    await context.db.save(mapping)
    await context.refresh_mappings()
    value = FloatData(value=39.0)
    execution = Node(value,func=child_function,is_async=True,parent_id=parent.id,parameters=Parameters())
    try:
        entry = await execution.make_registry_entry("", "new-child-arguments")
        assert entry.repository_revision == parent.repository_revision
        assert entry.repository_version == 1
        assert await execution._repository_revision() == parent.repository_revision
    finally:
        if execution.registry_entry is not None:
            await context.db.delete(execution.registry_entry)
        await context.db.delete(parent)
        await context.db.delete(mapping)
        await context.db.delete(value)
        await context.refresh_mappings()
