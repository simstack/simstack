import logging

from simstack.core.artifacts import ArtifactArguments, create_artifacts
from simstack.core.definitions import TaskStatus
from simstack.core.engine import current_engine_context
from simstack.core.node import node_from_database
from simstack.models import NodeRegistry, ArtifactModel
from simstack.models.charts_artifact import ChartArtifactModel
from simstack.models.table_artifact import TableArtifactModel
from simstack.util.importer import import_class
from simstack.util.importer import import_function

logger = logging.getLogger("recompute_artifacts")


async def recompute_artifacts(node_registry: NodeRegistry):
    """
    Recomputes artifacts for a node and all its children recursively.

    First processes all children nodes recursively, then removes and recomputes
    the current node's artifacts.

    :param node_registry: The registry entry for the node to process
    :type node_registry: NodeRegistry
    """
    # Create Node from the registry

    engine = current_engine_context.get()
    node = await node_from_database(node_registry)
    if node is None:
        logger.error(f"Failed to create node from registry task_id: {node_registry.id}")
        return

    if node.status != TaskStatus.COMPLETED:
        logger.error(f"Cannot recompute artifacts for task_id: {node_registry.id} name: {node.name} status: {node.status}")
        return

    # Find all children of this node
    children = await engine.find(NodeRegistry, NodeRegistry.parent_ids.in_([node_registry.id]))

    # Recursively recompute artifacts for all children first
    for child_registry in children:
        await recompute_artifacts(child_registry)

    # Remove current node's artifacts
    if node_registry.artifact_ids:
        logger.info(f"Removing {len(node_registry.artifact_ids)} artifacts for node {node_registry.id}")

        table_artifacts = await engine.find(
            TableArtifactModel, TableArtifactModel.parent_id == node_registry.id
        )
        for table_artifact in table_artifacts:
            await engine.delete(table_artifact)

        chart_artifacts = await engine.find(
            ChartArtifactModel, ChartArtifactModel.parent_id == node_registry.id
        )
        for chart_artifact in chart_artifacts:
            await engine.delete(chart_artifact)
        # Delete artifacts from the database
        for artifact_id in node_registry.artifact_ids:
            instance = await engine.find_one(
                ArtifactModel, ArtifactModel.id == artifact_id
            )
            if instance:
                await engine.delete(instance)
            else:
                logger.warning(
                    f"task_id: {node_registry.id} Failed to delete artifact {artifact_id} from database"
                )
        node_registry.artifact_ids = []

    # Recompute artifacts for this node
    if node_registry.status == TaskStatus.COMPLETED:
        logger.info(f"Recomputing artifacts for node {node_registry.id}")

        # Load the result to create new artifacts
        result = await node.load_results()
        if result is not None:
            artifact_arguments = ArtifactArguments(result, node_registry.id)
            # Reconstruct the function arguments for artifact creation
            args = []
            for table, table_id in zip(
                node_registry.input_tables, node_registry.input_ids
            ):
                model = await import_class(table)
                arg = await engine.find_one(model, model.id == table_id)
                if arg:
                    args.append(arg)

            # Get the function for artifact creation
            wrapped_func = await import_function(node_registry.func_mapping)
            func = (
                wrapped_func
                if not hasattr(wrapped_func, "_inner")
                else wrapped_func._inner
            )

            # Create node kwargs similar to run_local
            node_kwargs = {
                "parent_id": node_registry.id,
                "task_id": node_registry.id,
                "call_path": node_registry.call_path,
                "parent_parameters": node_registry.parameters,
            }

            artifact_arguments.add_attributes(func, *args, **node_kwargs)
            node_registry.artifact_ids = await create_artifacts(
                artifact_arguments, node_registry
            )

            # Save the updated registry
            await engine.save(node_registry)
            logger.info(
                f"Recomputed {len(node_registry.artifact_ids)} artifacts for node {node_registry.id}"
            )
    else:
        logger.warning(
            f"Node {node_registry.id} is not completed, cannot recompute artifacts"
        )
