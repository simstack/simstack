import inspect
import logging
import re
from typing import Any, Callable, Optional, List

from simstack.models.document import Model, ObjectId

from simstack.models.artifact_models import ArtifactMapping, ArtifactModel
from simstack.models.charts_artifact import ChartArtifactModel
from simstack.models.images2d import Image2DArtifactModel
from simstack.models.node_registry import NodeRegistry
from simstack.models.table_artifact import TableArtifactModel
from simstack.persistence import Repository, eq, in_values
from simstack.util.importer import import_function

logger = logging.getLogger("artifacts")

ARTIFACT_TYPES = (ArtifactModel, ChartArtifactModel, TableArtifactModel, Image2DArtifactModel)


async def find_child_nodes(
    repository: Repository,
    task_id: ObjectId,
) -> list[NodeRegistry]:
    return await repository.find(
        NodeRegistry,
        in_values("parent_ids", [task_id]),
    )


class ArtifactArguments:
    def __init__(self, result: Any, task_id: ObjectId | None = None) -> None:
        self.task_id = task_id
        self.result = result
        self.child_artifacts: list[Model] = []
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


async def find_artifact_mappings(
    node_registry_path: str, db: Repository, task_id: Optional[str] = None
) -> List[ArtifactMapping]:
    logger.debug(
        f"task_id: {task_id} Loading artifacts with regex pattern: {node_registry_path} "
    )
    all_mappings = await db.find(ArtifactMapping)

    # Filter them manually to find those whose patterns match your path
    matching_mappings = [
        mapping
        for mapping in all_mappings
        if re.match(mapping.regex_pattern, node_registry_path)
    ]

    for mapping in matching_mappings:
        logger.debug(
            f"task_id: {task_id} Artifact found: {mapping.name} with path: {mapping.regex_pattern}"
        )
    return matching_mappings


async def register_artifact_mapping(
    artifact_mapping: ArtifactMapping,
    repository: Repository,
) -> ArtifactMapping:
    # Check if an artifact with the same name already exists
    existing = await repository.find_one(
        ArtifactMapping, eq("name", artifact_mapping.name)
    )
    if existing:
        logger.warning(
            f"Replacing existing artifact mapping '{artifact_mapping.name}' already exists with path: {existing.regex_pattern}"
        )
        existing.set_values(artifact_mapping)
        artifact_mapping = await repository.save(existing)

    # Check if the function_mapping is specified and not CODE
    if (
        artifact_mapping.function_mapping
        and artifact_mapping.function_mapping != "CODE"
    ):
        try:
            # Import the function to verify it exists
            func = await import_function(
                artifact_mapping.function_mapping, repository, task_id=None
            )
            if not func:
                logger.warning(
                    "Could not import function {artifact_mapping.function_mapping}"
                )
        except Exception as e:
            logger.error(
                f"Error processing function {artifact_mapping.function_mapping}: {e}"
            )
    return await repository.save(artifact_mapping)


async def find_all_artifacts(
    node_registry: NodeRegistry, db: Repository
) -> list[Model]:
    artifacts: list[Model] = []
    for artifact_id in node_registry.artifact_ids:
        for model_class in ARTIFACT_TYPES:
            artifact = await db.find_one(model_class, eq("id", artifact_id))
            if artifact is not None:
                artifacts.append(artifact)
                break
        else:
            raise ValueError(f"Task artifact is missing: {artifact_id}")
    return artifacts


async def create_artifacts(
    artifact_arguments: ArtifactArguments,
    node_registry: NodeRegistry,
    repository: Repository,
) -> List[ObjectId]:
    try:
        call_path = node_registry.call_path
        task_id = node_registry.id
        log_string = f"create artifacts for task_id: {task_id} for {call_path}"

        artifact_mappings_list = await find_artifact_mappings(
            call_path, repository, task_id=task_id
        )

        child_nodes = await find_child_nodes(repository, task_id)
        logger.info(
            f"{log_string} Found {len(artifact_mappings_list)} artifact mappings for path: {call_path} and {len(child_nodes)} child nodes."
        )
        # Concatenate artifacts from all child nodes
        child_artifacts: list[Model] = []
        for child_node in child_nodes:
            loaded_artifacts = await find_all_artifacts(child_node, repository)
            if len(loaded_artifacts) == 1:
                child_artifacts.extend(loaded_artifacts)
            elif len(loaded_artifacts) > 1:
                # If multiple artifacts, consolidate them into a list
                consolidated_artifact = ArtifactModel(
                    name=child_node.name,
                    path=call_path,
                    data={
                        node_registry.name: loaded_artifacts
                    },  # Store all artifacts in a single field
                )
                await repository.save(consolidated_artifact)
                child_artifacts.append(consolidated_artifact)

        # child_artifacts = consolidate_artifacts(child_artifacts, call_path, task_id)
        artifact_arguments.child_artifacts = child_artifacts
        artifact_arguments.call_path = call_path

        artifact_list: list[Model] = []

        if len(artifact_mappings_list) > 0:
            for artifact_mapping in artifact_mappings_list:
                log_string_mapping = f"{log_string} Artifact: {artifact_mapping.name}"
                logging.info(log_string_mapping)
                if artifact_mapping.function_mapping != "CODE":
                    func = await import_function(
                        artifact_mapping.function_mapping,
                        repository,
                        artifact_arguments.task_id,
                    )
                    if not func:
                        raise ValueError(
                            f"{log_string_mapping} Function {artifact_mapping.function_mapping} not found for artifact mapping."
                        )
                    logger.info(
                        f"{log_string_mapping} Executing function {artifact_mapping.function_mapping} for artifact mapping."
                    )
                    artifact_result = func(artifact_arguments)
                    if inspect.isawaitable(artifact_result):
                        artifact_result = await artifact_result
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
                        raise ValueError(
                            f"{log_string_mapping} Code execution failed with error: {result['error']}"
                        )

                # An artifact result can be either an Artifact or a list of elements which can be Artifacts or ArtifactModels (from children)
                if not isinstance(artifact_result, List):
                    artifact_result = [artifact_result]

                for artifact in artifact_result:
                    if artifact is None:
                        logger.warning(
                            f"{log_string_mapping} Artifact is None, skipping."
                        )
                        continue
                    if isinstance(artifact, ARTIFACT_TYPES):
                        artifact.parent_id = node_registry.id
                        if isinstance(artifact, ArtifactModel):
                            artifact.path = call_path
                        saved_artifact = await repository.save(artifact)
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
    except Exception as e:
        logger.exception(f"Error creating artifacts for node {node_registry.name}: {e}")
        raise


async def find_artifacts(
    model_class: type[ArtifactModel],
    repository: Repository,
    **kwargs: Any,
) -> List[ArtifactModel]:
    """Find artifacts through backend-neutral equality criteria."""

    if model_class != ArtifactModel:
        discriminator_value = model_class.model_fields["artifact_type"].default
        kwargs["artifact_type"] = discriminator_value
    return await repository.find(
        model_class,
        *(eq(field, value) for field, value in kwargs.items()),
    )


# async def find_one_artifact(artifact_id: ObjectId) -> Optional[ArtifactModel]:
#     """
#     Find an artifact by ID and return it as the appropriate subclass instance
#     based on its artifact_type field.
#
#     Args:
#         artifact_id: The ObjectId of the item to find
#
#     Returns:
#         An instance of the appropriate ArtifactModel subclass, or None if not found
#     """
#     # Find the document by ID
#     doc = await collection.find_one({"_id": artifact_id})
#
#     if doc is None:
#         return None
#
#     # Determine the appropriate class based on artifact_type
#     artifact_type = doc.get("artifact_type")
#
#     # Map artifact_type values to their respective classes
#     type_to_class = {
#         "int": IntArtifactModel,
#         "float": FloatArtifactModel,
#         "str": StringArtifactModel,
#         "list": List,
#         # Add more mappings as you add more subclasses
#     }
#
#     # Get the appropriate class, defaulting to base ArtifactModel if the type is unknown
#     model_class = type_to_class.get(artifact_type, ArtifactModel)
#
#     # For List type, we need special handling to load items
#     if artifact_type == "list":
#         # Store item_ids temporarily
#         item_ids = doc.get("items", [])
#         # Remove items field for now to allow validation
#         doc["items"] = []
#
#         # Create the List instance without items first
#         item = model_class.model_validate(doc)
#
#         # load all referenced items
#         loaded_items = []
#         for artifact_id in item_ids:
#             if nested_item:
#                 loaded_items.append(nested_item)
#
#         # Now assign the loaded items
#         item.items = loaded_items
#     else:
#         # For non-List types, normal validation is fine
#         item = model_class.model_validate(doc)
#
#
#     return item

#
# async def load_list_items(list_item: List) -> List[ArtifactModel]:
#     """
#     Load all items in a List type and return them as a Python list.
#     This is now a simple accessor since items are already ArtifactModel objects.
#
#     Args:
#         list_item: A List instance
#
#     Returns:
#         A list of ArtifactModel instances
#     """
#     if not isinstance(list_item, List):
#         raise TypeError("Expected a List instance")
#
#     return list_item.items
#
