import importlib
import inspect
import logging
import re
from typing import Optional, List

from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.engine import current_engine_context
from simstack.models.artifact_models import ArtifactMapping, ArtifactModel
from simstack.models.charts_artifact import ChartArtifactModel
from simstack.models.node_registry import find_child_nodes, NodeRegistry
from simstack.models.table_artifact import TableArtifactModel
from simstack.util.importer import function_from_model
from simstack.util.module_path_checker import is_module_subpath_of_path

logger = logging.getLogger("artifacts")


class ArtifactArguments:
    def __init__(self, result, task_id: ObjectId = None):
        self.task_id = task_id
        self.result = result
        self.child_artifacts = []

    def add_attributes(self, func, *args, **kwargs):
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
    node_registry_path: str, task_id: Optional[str] = None
) -> List[ArtifactMapping]:
    logger.debug(
        f"task_id: {task_id} Loading artifacts with regex pattern: {node_registry_path} "
    )
    engine = current_engine_context.get()
    all_mappings = await engine.find(ArtifactMapping)

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


async def register_artifact_mapping(artifact_mapping: ArtifactMapping):
    # Check if an artifact with the same name already exists
    existing = await context.db.engine.find_one(
        ArtifactMapping, ArtifactMapping.name == artifact_mapping.name
    )
    if existing:
        logger.warning(
            f"Replacing existing artifact mapping '{artifact_mapping.name}' already exists with path: {existing.regex_pattern}"
        )
        existing.set_values(artifact_mapping)
        artifact_mapping = await context.db.save(existing)

    # Check if the function_mapping is specified and not CODE
    if (
        artifact_mapping.function_mapping
        and artifact_mapping.function_mapping != "CODE"
    ):
        try:
            # Import the function to verify it exists
            module_path, function_name = artifact_mapping.function_mapping.rsplit(
                ".", 1
            )
            module = importlib.import_module(module_path)
            if module is None:
                logger.error(f"Module {module_path} could not be imported.")
                raise ImportError(f"Module {module_path} could not be imported.")
            # Get the function from the module
            func = getattr(module, function_name)

            if func:
                # Function import succeeded, now check if it's in a path with use_pickle=True
                # Check if the module path is in a non-excluded subdirectory of any path in the path registry
                for path_name, path_info in context.path_manager.paths.items():
                    # Check if the path has use_pickle=True
                    current_module_is_child = is_module_subpath_of_path(
                        module_path, path_info["path"]
                    )
                    if path_info.get("use_pickle", True) and current_module_is_child:
                        # create a FunctionPickle for the function
                        try:
                            from simstack.models.pickle_models import FunctionPickle

                            # Create a FunctionPickle for the function
                            function_pickle = FunctionPickle(
                                name=func.__name__, module_path=func.__module__
                            )
                            function_pickle.store_function(func)

                            # Save the FunctionPickle to the database
                            await context.db.save(function_pickle)

                            # Associate the FunctionPickle with the ArtifactMapping
                            artifact_mapping.pickle_function = function_pickle

                            logger.info(
                                f"Created FunctionPickle for {artifact_mapping.function_mapping}"
                            )
                            break  # We only need to create one FunctionPickle
                        except Exception as e:
                            logger.error(
                                f"Error creating FunctionPickle for {artifact_mapping.function_mapping}: {e}"
                            )
            else:
                logger.warning(
                    f"Could not import function {artifact_mapping.function_mapping}"
                )
        except Exception as e:
            logger.error(
                f"Error processing function {artifact_mapping.function_mapping}: {e}"
            )
    return await context.db.save(artifact_mapping)


async def find_all_artifacts(node_registry: NodeRegistry) -> List[ArtifactModel]:
    # if not engine:
    #     engine = context.db.engine
    engine = current_engine_context.get()
    return [
        await engine.find_one(ArtifactModel, ArtifactModel.id == artifact_id)
        for artifact_id in node_registry.artifact_ids
    ]


async def create_artifacts(
    artifact_arguments: ArtifactArguments, node_registry: NodeRegistry
) -> List[ArtifactModel]:
    try:
        call_path = node_registry.call_path
        task_id = node_registry.id
        log_string = f"create artifacts for task_id: {task_id} for {call_path}"

        artifact_mappings_list = await find_artifact_mappings(
            call_path, task_id=task_id
        )

        child_nodes = await find_child_nodes(task_id)
        logger.info(
            f"{log_string} Found {len(artifact_mappings_list)} artifact mappings for path: {call_path} and {len(child_nodes)} child nodes."
        )
        # Concatenate artifacts from all child nodes
        child_artifacts = []
        for child_node in child_nodes:
            loaded_artifacts = await find_all_artifacts(child_node)
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
                await context.db.save(consolidated_artifact)
                child_artifacts.append(consolidated_artifact)

        # child_artifacts = consolidate_artifacts(child_artifacts, call_path, task_id)
        artifact_arguments.child_artifacts = child_artifacts
        artifact_arguments.call_path = call_path

        artifact_list = []
        engine = current_engine_context.get()

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
                    func = await function_from_model(
                        artifact_mapping, artifact_arguments.task_id
                    )
                    if not func:
                        logger.error(
                            f"{log_string_mapping} Function {artifact_mapping.function_mapping} not found for artifact mapping."
                        )
                        continue
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
                    if isinstance(artifact, TableArtifactModel):
                        artifact.parent_id = node_registry.id
                        saved_artifact = await engine.save(artifact)
                        logger.debug(
                            f"{log_string_mapping} new table: {saved_artifact}"
                        )
                    elif isinstance(artifact, ChartArtifactModel):
                        artifact.parent_id = node_registry.id
                        saved_artifact = await engine.save(artifact)
                        logger.debug(
                            f"{log_string_mapping} new table: {saved_artifact}"
                        )
                    elif isinstance(artifact, ArtifactModel):
                        artifact.path = call_path
                        saved_artifact = await engine.save(artifact)
                        logger.debug(f"{log_string_mapping} new: {saved_artifact}")
                        artifact_list.append(saved_artifact)
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
        return []


async def save_artifact_model(artifact: ArtifactModel) -> ArtifactModel:
    """
    Custom save function for polymorphic models

    If the item is a List type, it will also save any unsaved nested items.
    """
    # Special handling for List type
    if artifact.type == "list":
        # Save all nested items first
        saved_items = []
        for nested_item in artifact.items:
            # Save the nested item
            saved_item = await save_artifact_model(nested_item)
            saved_items.append(saved_item)

        # Update the items list with all saved items
        artifact.items = saved_items

    # Get the model's collection
    collection = context.db.engine.get_collection(ArtifactModel)

    # Convert model to dict
    item_dict = artifact.model_dump(by_alias=True)

    # For List types, we need to convert ArtifactModel objects to their IDs for storage
    if artifact.type == "list":
        # Replace ArtifactModel objects with their IDs in the dict for storage
        item_dict["items"] = [nested_item.id for nested_item in artifact.items]

    # Check if this artifact already exists in the database
    if artifact.id is not None:
        existing = await collection.find_one({"_id": artifact.id})
        if existing:
            # ArtifactModel exists, update it
            await collection.replace_one({"_id": artifact.id}, item_dict)
            return artifact

    # ArtifactModel doesn't exist or has no ID, insert it
    # Remove id field if it exists to let MongoDB generate it
    if "_id" in item_dict:
        del item_dict["_id"]

    result = await collection.insert_one(item_dict)
    artifact.id = result.inserted_id

    return artifact


async def find_artifacts(
    model_class: type[ArtifactModel], **kwargs
) -> List[ArtifactModel]:
    """
    Custom find function for polymorphic models
    """
    # Get the collection
    collection = context.db.engine.get_collection(ArtifactModel)

    # Add item_type filter for subclasses
    if model_class != ArtifactModel:
        discriminator_value = model_class.model_fields["artifact_type"].default
        kwargs["artifact_type"] = discriminator_value

    # Execute find
    cursor = collection.find(kwargs)
    results = await cursor.to_list(length=None)

    # Convert results to model instances
    items = []
    for doc in results:
        artifact_type = doc.get("artifact_type")

        # For List types, we need special handling
        if artifact_type == "list":
            # Store item_ids temporarily
            item_ids = doc.get("items", [])
            # Remove items field for validation
            doc_copy = doc.copy()
            doc_copy["items"] = []

            # Create the List instance without items first
            item = model_class.model_validate(doc_copy)

            # Load all referenced items
            loaded_items = []
            for item_id in item_ids:
                nested_item = await context.db.find_one(
                    ArtifactModel, ArtifactModel.id == item_id
                )
                if nested_item:
                    loaded_items.append(nested_item)

            # Now assign the loaded items
            item.items = loaded_items
        else:
            # For non-List types, normal validation is fine
            item = model_class.model_validate(doc)

    items.append(item)

    return items


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
#     # Get the collection
#     collection = context.db.engine.get_collection(ArtifactModel)
#
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
#             nested_item = await context.db.find_one(ArtifactModel, ArtifactModel.id == artifact_id)
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
