import inspect
import logging
from typing import Any, Callable, List

from simstack.models.document import ObjectId

from simstack.core.context import context
from simstack.models.artifact_models import ArtifactMapping, ArtifactModel
from simstack.models.charts_artifact import ChartArtifactModel
from simstack.models.images2d import Image2DArtifactModel
from simstack.models.node_registry import find_child_nodes, NodeRegistry
from simstack.models.table_artifact import TableArtifactModel
from simstack.util.importer import import_artifact_generator
from simstack.server_api.http_transport import RunnerAPIError

logger = logging.getLogger("artifacts")


class ArtifactArguments:
    def __init__(self, result: Any, task_id: ObjectId | None = None) -> None:
        self.task_id = task_id
        self.result = result
        self.child_artifacts: list[ArtifactModel] = []
        self.call_path: str | None = None

    def add_attributes(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> None:
        if not callable(func):
            raise ValueError("func must be callable")
        # Get function parameter names
        sig = inspect.signature(func)
        parameters = list(sig.parameters.keys())
        # Map positional args to parameter names
        for param_name, arg_value in zip(parameters, args):
            setattr(self, param_name, arg_value)
        # Map keyword args directly
        for param_name, arg_value in kwargs.items():
            setattr(self, param_name, arg_value)


async def find_artifact_mappings(node_registry_path: str, api=None, task_id=None):
    return await (api or context.server.values).artifact_mappings(node_registry_path)


async def register_artifact_mapping(artifact_mapping: ArtifactMapping) -> ArtifactMapping:
    if artifact_mapping.function_mapping and artifact_mapping.function_mapping != "CODE":
        await import_artifact_generator(artifact_mapping)
    return await context.server.values.register_artifact_mapping(artifact_mapping)


async def find_all_artifacts(node_registry: NodeRegistry, api=None):
    client = api or context.server.values
    return await client.resolve_values([
        {"mapping": "simstack.models.artifact_models.ArtifactModel", "id": str(identity)}
        for identity in node_registry.artifact_ids
    ])


async def create_artifacts(
    artifact_arguments: ArtifactArguments, node_registry: NodeRegistry
) -> List[ObjectId]:
    try:
        call_path = node_registry.call_path
        task_id = node_registry.id
        log_string = f"create artifacts for task_id: {task_id} for {call_path}"

        artifact_mappings_list = await find_artifact_mappings(
            call_path, context.server.values, task_id=task_id
        )

        child_nodes = await find_child_nodes(task_id)
        logger.info(
            f"{log_string} Found {len(artifact_mappings_list)} artifact mappings for path: {call_path} and {len(child_nodes)} child nodes."
        )
        # Concatenate artifacts from all child nodes
        child_artifacts: list[ArtifactModel] = []
        for child_node in child_nodes:
            loaded_artifacts = await find_all_artifacts(child_node, context.server.values)
            if len(loaded_artifacts) == 1:
                child_artifacts.extend(loaded_artifacts)
            elif len(loaded_artifacts) > 1:
                # If multiple artifacts, consolidate them into a list
                consolidated_artifact = ArtifactModel(
                    name=child_node.name,
                    parent_id=node_registry.id,
                    path=call_path,
                    data={
                        node_registry.name: loaded_artifacts
                    },  # Store all artifacts in a single field
                )
                await context.server.values.publish_value(consolidated_artifact)
                child_artifacts.append(consolidated_artifact)

        # child_artifacts = consolidate_artifacts(child_artifacts, call_path, task_id)
        artifact_arguments.child_artifacts = child_artifacts
        artifact_arguments.call_path = call_path

        artifact_list: list[ArtifactModel] = []

        if len(artifact_mappings_list) > 0:
            for artifact_mapping in artifact_mappings_list:
                # Count existing artifacts with the same mapping
                existing_count = len(
                    [a for a in artifact_list if a.path.endswith(artifact_mapping.name)]
                )
                log_string_mapping = (
                    f"{log_string} Artifact: {artifact_mapping.name} #:{existing_count}"
                )
                logging.info(log_string_mapping)
                if artifact_mapping.function_mapping != "CODE":
                    func = await import_artifact_generator(artifact_mapping, task=node_registry)
                    logger.info(
                        f"{log_string_mapping} Executing function {artifact_mapping.function_mapping} for artifact mapping."
                    )
                    artifact_result = func(artifact_arguments)
                else:
                    from simstack.util.safe_code_executor import safe_code_executor

                    result = safe_code_executor(
                        artifact_mapping.function_code, artifact_arguments
                    )
                    logger.info(
                        f"{log_string_mapping} Executing code for artifact mapping."
                    )
                    if result["success"]:
                        artifact_result = result["result"]
                    else:
                        logger.error(
                            f"{log_string_mapping} Code execution failed with error: {result['error']}"
                        )
                        continue

                # An artifact result can be either an Artifact or a list of elements which can be Artifacts or ArtifactModels (from children)
                if not isinstance(artifact_result, List):
                    artifact_result = [artifact_result]

                for artifact in artifact_result:
                    if artifact is None:
                        logger.warning(
                            f"{log_string_mapping} Artifact is None, skipping."
                        )
                        continue
                    if isinstance(artifact, (TableArtifactModel, ChartArtifactModel, ArtifactModel, Image2DArtifactModel)):
                        artifact.parent_id = node_registry.id
                        if isinstance(artifact, ArtifactModel):
                            artifact.path = call_path
                        saved_artifact = await context.server.values.publish_value(artifact)
                        if isinstance(artifact, ArtifactModel):
                            artifact_list.append(saved_artifact)
                        logger.debug(
                            f"{log_string_mapping} new artifact: {saved_artifact}"
                        )
                    else:
                        raise ValueError(
                            f"{log_string_mapping} not an ArtifactModel object. Got {artifact} instead."
                        )
        else:
            artifact_list = child_artifacts
            logger.debug(f"{log_string} passing child artifacts")
        return [artifact.id for artifact in artifact_list]
    except RunnerAPIError:
        raise
    except Exception as e:
        logger.exception(f"Error creating artifacts for node {node_registry.name}: {e}")
        return []


async def save_artifact_model(artifact: ArtifactModel) -> ArtifactModel:
    """Persist a polymorphic artifact through the neutral model API."""
    if getattr(artifact, "type", None) == "list":
        artifact.items = [await save_artifact_model(item) for item in artifact.items]
    return await context.server.values.publish_value(artifact)


async def find_artifacts(model_class: type[ArtifactModel], **kwargs: Any) -> List[ArtifactModel]:
    if model_class != ArtifactModel and "artifact_type" in model_class.model_fields:
        kwargs["artifact_type"] = model_class.model_fields["artifact_type"].default
    return await context.server.values.query_artifacts(model_class, **kwargs)
