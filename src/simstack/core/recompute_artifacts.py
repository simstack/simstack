import logging

from simstack.core.artifacts import ARTIFACT_TYPES, ArtifactArguments, create_artifacts
from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.node_persistence import reconstruct_persisted_node
from simstack.models import NodeRegistry
from simstack.models.document import Model
from simstack.persistence import Repository, eq, in_values
from simstack.util.importer import import_class
from simstack.util.importer import import_function

logger = logging.getLogger("recompute_artifacts")


async def recompute_artifacts(
    node_registry: NodeRegistry,
    repository: Repository,
    *,
    _pending_cleanup: list[Model] | None = None,
) -> None:
    """
    Recomputes artifacts for a node and all its children recursively.

    Build replacements before switching the task's artifact references. Failed
    collectors leave the previous references and documents available.

    :param node_registry: The registry entry for the node to process
    :type node_registry: NodeRegistry
    """
    cleanup_root = _pending_cleanup is None
    if _pending_cleanup is None:
        _pending_cleanup = []

    node = await reconstruct_persisted_node(node_registry, repository)

    if node.status != TaskStatus.COMPLETED:
        logger.error(f"Cannot recompute artifacts for task_id: {node_registry.id} name: {node.name} status: {node.status}")
        return

    # Find all children of this node
    children = await repository.find(
        NodeRegistry, in_values("parent_ids", [node_registry.id])
    )

    # Recursively recompute artifacts for all children first
    for child_registry in children:
        await recompute_artifacts(child_registry, repository, _pending_cleanup=_pending_cleanup)

    old_ids = list(node_registry.artifact_ids)
    old_owned = [
        artifact
        for artifact_type in ARTIFACT_TYPES
        for artifact in await repository.find(artifact_type, eq("parent_id", node_registry.id))
        if artifact.id in old_ids
    ]

    # Recompute artifacts for this node
    if node_registry.status == TaskStatus.COMPLETED:
        logger.info(f"Recomputing artifacts for node {node_registry.id} {node_registry.name}")

        # Load the result to create new artifacts
        result = await node.load_results()
        if result is not None:
            artifact_arguments = ArtifactArguments(result, node_registry.id)
            # Reconstruct the function arguments for artifact creation
            args = []
            for ref in node_registry.input_references:
                model = await import_class(ref.variable_mapping, repository)
                arg = await repository.find_one(model, eq("id", ref.reference))
                if arg:
                    args.append(arg)

            # Get the function for artifact creation
            wrapped_func = await import_function(
                node_registry.func_mapping, repository
            )
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
            replacement_ids = await create_artifacts(
                artifact_arguments,
                node_registry,
                repository,
            )

        else:
            replacement_ids = []
        updated = await repository.update_node_registry(
            node_registry.id,
            {"artifact_ids": replacement_ids},
            expected={"status": TaskStatus.COMPLETED, "artifact_ids": old_ids},
        )
        if updated is None:
            raise RuntimeError(f"Task {node_registry.id} artifacts changed concurrently")
        node_registry.artifact_ids = list(updated.artifact_ids)
        _pending_cleanup.extend(artifact for artifact in old_owned if artifact.id not in replacement_ids)
        if cleanup_root:
            # Parents may still point at their children's previous artifacts
            # during recursion. Collect only after the entire requested tree
            # has switched its references.
            for artifact in _pending_cleanup:
                references = await repository.find(
                    NodeRegistry, in_values("artifact_ids", [artifact.id]), limit=1,
                )
                if not references:
                    await repository.delete(artifact)
        logger.info(
            "Recomputed %s artifacts for node %s", len(replacement_ids), node_registry.id,
        )
    else:
        logger.warning(
            f"Node {node_registry.id} is not completed, cannot recompute artifacts"
        )
