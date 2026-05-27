import importlib
import logging
from typing import Callable, Optional, Type
from odmantic import Model, AIOEngine, ObjectId

from simstack.core.context import context
from simstack.models.models import ModelMapping, NodeModel
from simstack.util.db import Database

logger = logging.getLogger("importer")

NODES_SEARCH_BY_NAME_FALLBACK = True
MODELS_SEARCH_BY_NAME_FALLBACK = True


def _get_initialized_context():
    try:
        from simstack.core.context import context

        if context.initialized:
            return context
    except RuntimeError:
        return None
    return None


def _context_cache_matches_engine(context, db: Database = None) -> bool:
    if context is None:
        return False
    if db is None:
        return True
    try:
        return context.db is not None and db is context.db
    except RuntimeError:
        return False

# TODO remove engine function
def _resolve_engine(context, engine: AIOEngine = None):
    if engine is not None:
        return engine
    if context is not None:
        try:
            if context.db is not None:
                return context.db.engine
        except RuntimeError:
            pass
    raise RuntimeError("Could not resolve engine both engine and context have no engine")


def _lookup_node_cache(node_mappings, function_path: str) -> Optional[NodeModel]:
    if node_mappings is None:
        return None
    node_model = node_mappings.get_by_mapping(function_path)
    if node_model is None and NODES_SEARCH_BY_NAME_FALLBACK:
        _, function_name = function_path.rsplit(".", 1)
        node_model = node_mappings.get_by_name(function_name)
    return node_model


async def _find_node_model(function_path: str, db: Database) -> Optional[NodeModel]:
    if context.node_mappings is None:
        await context.refresh_mappings(models=False, nodes=True)

    node_model = _lookup_node_cache(context.node_mappings, function_path)
    if node_model is not None:
        return node_model

    await context.refresh_mappings(models=False, nodes=True)
    node_model = _lookup_node_cache(context.node_mappings, function_path)
    if node_model is not None:
        return node_model

    node_model = await db.find_one(
        NodeModel, NodeModel.function_mapping == function_path
    )
    if node_model is None and NODES_SEARCH_BY_NAME_FALLBACK:
        _, function_name = function_path.rsplit(".", 1)
        node_model = await db.find_one(
            NodeModel, NodeModel.name == function_name
        )
    return node_model


async def _find_node_model_by_name(function_name: str, db: Database) -> Optional[NodeModel]:
    if context.node_mappings is None:
        await context.refresh_mappings(models=False, nodes=True)

    node_model = context.node_mappings.get_by_name(function_name)
    if node_model is not None:
        return node_model

    await context.refresh_mappings(models=False, nodes=True)
    node_model = context.node_mappings.get_by_name(function_name)
    if node_model is not None:
        return node_model
    return await db.find_one(NodeModel, NodeModel.name == function_name)


def _lookup_model_cache(model_mappings, class_path: str, class_name: str) -> Optional[ModelMapping]:
    if model_mappings is None:
        return None
    model_mapping = None
    if MODELS_SEARCH_BY_NAME_FALLBACK:
        model_mapping = model_mappings.get_by_name(class_name)
    if not model_mapping:
        model_mapping = model_mappings.get_by_mapping(class_path)
    return model_mapping

async def _find_model_mapping(class_path: str, db: Database) -> Optional[ModelMapping]:
    _, class_name = class_path.rsplit(".", 1)

    if context.model_mappings is None:
        await context.refresh_mappings(models=True, nodes=False)

    model_mapping = _lookup_model_cache(context.model_mappings, class_path, class_name)
    if model_mapping is not None:
        return model_mapping

    await context.refresh_mappings(models=True, nodes=False)
    model_mapping = _lookup_model_cache(context.model_mappings, class_path, class_name)
    if model_mapping is not None:
        return model_mapping

    model_mapping = None
    if MODELS_SEARCH_BY_NAME_FALLBACK:
        model_mapping = await db.find_one(ModelMapping, ModelMapping.name == class_name)
    if model_mapping is None:
        model_mapping = await db.find_one(ModelMapping, ModelMapping.mapping == class_path)
    return model_mapping

# TODO engines remove: duplicate of find_class_mapping_by_name
async def _find_model_mapping_by_name(class_name: str, db: Database) -> Optional[ModelMapping]:

    if context.model_mappings is None:
        await context.refresh_mappings(models=True, nodes=False)

    model_mapping = context.model_mappings.get_by_name(class_name)
    if model_mapping is not None:
        return model_mapping

    await context.refresh_mappings(models=True, nodes=False)
    model_mapping = context.model_mappings.get_by_name(class_name)
    if model_mapping is not None:
        return model_mapping

    return await db.find_one(ModelMapping, ModelMapping.name == class_name)


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
        logger.debug(
            f"task_id: {task_id} loading function {function_path} using regular import"
        )
    # Import the module
    module = importlib.import_module(module_path)
    # Get the function from the module
    function = getattr(module, function_name)
    return function


async def import_function(
    function_path: str,
    db: Database,
    task_id: ObjectId = None,
    tolerate_missing_function: bool = False,
) -> Optional[Callable]:
    """
    Dynamically import a function from a module using its full path, including a migration mechanism.
    load the function information using NodeModel
    load the pickled version if it exists
    if there is no pickled version, use regular import.

    Args:
        function_path: Dot notation path to the function (e.g. 'methods.submodule.function_name')
        db: Database object
        task_id: Optional task id
        tolerate_missing_function: If True, return None if function is not found, otherwise raise exception

    Returns:
        The imported function object or None if import fails
    """
    node_model = await _find_node_model(function_path, db)

    if node_model is None:
        raise LookupError(f"task_id: {task_id} Function {function_path} not found in the NodeModel Table")

    try:
        return await function_from_model(node_model, task_id)
    except Exception as e:
        if tolerate_missing_function:
            return None
        else:
            raise e


async def import_function_by_name(function_name: str, db: Database, task_id: ObjectId) -> Optional[Callable]:
    node_model = await _find_node_model_by_name(function_name, db)

    if node_model is None:
        logger.error(f"Could not find function mapping for name: {function_name}")
        raise ValueError(f"Could not find function mapping for name: {function_name}")

    return await function_from_model(node_model, task_id)


async def import_class(class_path: str, db: Database) -> Type[Model] | None:
    """
    Dynamically import a class from a module using its full path.
    First tries to load the class from the database using ModelMapping

    A pickled version of the class is used primarily


    Args:

        :param class_path:   class_path: Dot notation path to the class (e.g. 'models.submodule.ClassName')
        :param db:    db: Database object
    Returns:
        The imported class object or None if import fails
    """

    try:
        # Split the path into module path and class name
        module_path, class_name = class_path.rsplit(".", 1)
        model_mapping = await _find_model_mapping(class_path, db)

        engine = current_engine_context.get()
        # If not found by name, try by mapping
        if not model_mapping:
            model_mapping = await db.find_one(
                ModelMapping, ModelMapping.mapping == class_path
            )
        else:  # when searching by name, the path may have changed
            module_path, class_name = model_mapping.mapping.rsplit(".", 1)

        if model_mapping is None:
            logger.error(f"Error finding ModelMapping for {class_name}")
            raise LookupError(f"Error finding ModelMapping for {class_name}")

        # Import the module
        module = importlib.import_module(module_path)

        # Get the class from the module
        return getattr(module, class_name)
    except (ImportError, AttributeError, ValueError) as e:
        logger.error(f"Error importing class {class_path}: {e}")
        raise e


async def import_class_by_name(class_name: str, db: Database) -> Type[Model]:
    model_mapping = await _find_model_mapping_by_name(class_name, db)

    if not model_mapping:
        logger.error(f"Error finding ModelMapping for {class_name}")
        raise LookupError(f"Error finding ModelMapping for {class_name}")

    return await import_class(model_mapping.mapping, db)
