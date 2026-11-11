import logging

from simstack.core.artifacts import ArtifactArguments, create_artifacts
from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_task
from simstack.models import NodeRegistry
from simstack.util.importer import import_function

logger = logging.getLogger("recompute_artifacts")


async def recompute_artifacts(node_registry: NodeRegistry):
    node = await node_from_task(node_registry)
    if node is None or node_registry.status != TaskStatus.COMPLETED:
        logger.error("Cannot recompute artifacts for non-completed task %s", node_registry.id)
        return
    for child in await context.server.tasks.children(node_registry.id):
        await recompute_artifacts(child)
    result = await node.load_results()
    if result is None:
        return
    arguments = ArtifactArguments(result, node_registry.id)
    inputs = node._args
    wrapped = await import_function(node_registry.func_mapping, context.server.values)
    function = getattr(wrapped, "_inner", wrapped)
    arguments.add_attributes(function, *inputs, parent_id=node_registry.id,
        task_id=node_registry.id, call_path=node_registry.call_path,
        parent_parameters=node_registry.parameters)
    await context.server.values.clear_generated_artifacts(node_registry)
    artifact_ids = await create_artifacts(arguments, node_registry)
    await context.server.values.replace_artifacts(node_registry, artifact_ids)
    node_registry.artifact_ids = artifact_ids
