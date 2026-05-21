import importlib
import logging
from typing import Callable, Optional, Type

from odmantic import Model, AIOEngine, ObjectId
from simstack.core.engine import current_engine_context
from simstack.models.models import ModelMapping, NodeModel

logger = logging.getLogger("importer")

NODES_SEARCH_BY_NAME_FALLBACK = True
MODELS_SEARCH_BY_NAME_FALLBACK = True

async def function_from_model(model, task_id: Optional[ObjectId] = None) -> Optional[Callable]:
    """
    Loads and retrieves a callable function from a specified model using dynamic import.
    If a task ID is specified, additional logging information is provided regarding the
    function retrieval process.

    Args:
        model: The model containing the function mapping which specifies the path to the
               desired function in dot notation.
        task_id (Optional[ObjectId]): Optional parameter specifying the task ID for logging
                                      purposes. Default is None.

    Returns:
        Optional[Callable]: The loaded callable function if successful, or None if the
                            function cannot be found.
    """
    function_path = model.function_mapping
    module_path, function_name = function_path.rsplit(".", 1)

    if task_id:
        logger.debug(f"task_id: {task_id} loading function {function_path} using regular import")
    # Import the module
    module = importlib.import_module(module_path)
    # Get the function from the module
    function = getattr(module, function_name)
    return function


async def import_function(
    function_path: str, task_id: ObjectId = None, tolerate_missing_function: bool = False
) -> Optional[Callable]:
    """
    Dynamically import a function from a module using its full path, including a migration mechanism.
    load the function information using NodeModel
    load the pickled version if it exists
    if there is no pickled version, use regular import.

    Args:
        function_path: Dot notation path to the function (e.g. 'methods.submodule.function_name')
        task_id: Optional task id
        tolerate_missing_function: If True, return None if function is not found, otherwise raise exception

    Returns:
        The imported function object or None if import fails
    """
    engine = current_engine_context.get()

    node_model = await engine.find_one(
        NodeModel, NodeModel.function_mapping == function_path
    )
    if node_model is None and NODES_SEARCH_BY_NAME_FALLBACK:
        _, function_name = function_path.rsplit(".", 1)
        node_model = await engine.find_one(NodeModel, NodeModel.name == function_name)

    if node_model is None:

        raise LookupError(
            f"task_id: {task_id} Function {function_path} not found in the NodeModel Table"
        )

    try:
        return await function_from_model(node_model, task_id)
    except Exception as e:
        if tolerate_missing_function:
            return None
        else:
            raise e

async def import_function_by_name(function_name: str, task_id: ObjectId, engine: AIOEngine = None) -> Optional[Callable]:
    if not engine:
        engine = current_engine_context.get()

    node_model = await engine.find_one(NodeModel, NodeModel.name == function_name)
    if node_model is None:
        logger.error(f"Could not find function mapping for name: {function_name}")
        raise ValueError(f"Could not find function mapping for name: {function_name}")

    return await function_from_model(node_model, task_id)

async def import_class(class_path: str) -> Type[Model] | None:
    """
    Dynamically import a class from a module using its full path.
    First tries to load the class from the database using ModelMapping

    A pickled version of the class is used primarily


    Args:
        class_path: Dot notation path to the class (e.g. 'models.submodule.ClassName')
        :param class_path:
    Returns:
        The imported class object or None if import fails
    """

    try:
        engine = current_engine_context.get()
        # Split the path into module path and class name
        module_path, class_name = class_path.rsplit(".", 1)
        model_mapping = await engine.find_one(
            ModelMapping, ModelMapping.name == class_name
        )

       # If not found by name, try by mapping
        if not model_mapping:
            model_mapping = await engine.find_one(
                ModelMapping, ModelMapping.mapping == class_path
            )
        else:  # when searching by name, the path may have changed
            module_path, class_name = model_mapping.mapping.rsplit(".", 1)

        if not model_mapping:
            logger.error(f"Error finding ModelMapping for {class_name}")
            raise LookupError(f"Error finding ModelMapping for {class_name}")

        # Import the module
        module = importlib.import_module(module_path)

        # Get the class from the module
        return getattr(module, class_name)
    except (ImportError, AttributeError, ValueError) as e:
        logger.error(f"Error importing class {class_path}: {e}")
        raise e


async def import_class_by_name(class_name: str) -> Type[Model]:
    engine = current_engine_context.get()
    model_mapping = await engine.find_one(ModelMapping, ModelMapping.name == class_name)

    if not model_mapping:
        logger.error(f"Error finding ModelMapping for {class_name}")
        raise LookupError(f"Error finding ModelMapping for {class_name}")

    return await import_class(model_mapping.mapping)
