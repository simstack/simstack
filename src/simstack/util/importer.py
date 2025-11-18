import importlib
import inspect
import logging
from typing import Callable, Optional, Type
from odmantic import Model, AIOEngine, ObjectId
from simstack.core.context import context
from simstack.core.engine import current_engine_context
import json
from simstack.models.models import ModelMapping, NodeModel

logger = logging.getLogger("importer")


async def _register_external_module_models(module_path: str, engine: AIOEngine):
    """
    Lazily register models from an external module using importlib.
    This works for any installed Python package, not just local files.
    """
    try:
        logger.debug(f"Attempting external module import for models: {module_path}")

        # Try to import the module
        module = importlib.import_module(module_path)

        # Get all classes from the module
        classes = inspect.getmembers(module, inspect.isclass)

        for class_name, new_class in classes:
            # Only process classes defined in this module (not imported ones)
            if new_class.__module__ != module.__name__:
                continue

            # Check if it's a Model or UIModel class
            bases = [base.__name__ for base in new_class.__bases__]

            # Import the model checking function
            from simstack.models.simstack_model import is_simstack_model
            is_ui_model = any("UIModel" in s for s in bases) or is_simstack_model(new_class)
            is_model = any(s == "Model" for s in bases)

            if not (is_model or is_ui_model):
                continue
            if class_name == "Model":
                continue

            logger.info(f"Registering external model: {class_name} from {module_path}")

            # Create the mapping using the full module path (no drops for external modules)
            full_mapping = f"{module_path}.{class_name}"

            # Check if already exists
            existing_entry = await engine.find_one(ModelMapping, ModelMapping.name == class_name)
            if existing_entry:
                if existing_entry.mapping == full_mapping:
                    logger.debug(f"External model {class_name} already registered")
                    continue
                else:
                    # Different mapping, delete and re-create
                    await engine.delete(existing_entry)

            # Create new ModelMapping entry (no pickle for external modules)
            if is_ui_model:
                try:
                    json_schema = json.dumps(new_class.json_schema())
                    ui_schema = json.dumps(new_class.ui_schema())
                except Exception as schema_error:
                    logger.warning(f"Could not generate schema for {class_name}: {schema_error}")
                    json_schema = "{}"
                    ui_schema = "{}"

                model_entry = ModelMapping(
                    name=class_name,
                    mapping=full_mapping,
                    collection_name=getattr(new_class, "__collection__", None),
                    json_schema=json_schema,
                    ui_schema=ui_schema,
                    route="",
                    pickle_class=None  # No pickle for external modules
                )
            else:
                model_entry = ModelMapping(
                    name=class_name,
                    mapping=full_mapping,
                    collection_name=getattr(new_class, "__collection__", None),
                    pickle_class=None  # No pickle for external modules
                )

            await engine.save(model_entry)
            logger.debug(f"Successfully registered external model: {class_name}")

    except ImportError as e:
        logger.debug(f"Could not import external module {module_path}: {e}")
    except Exception as e:
        logger.debug(f"Error registering models from external module {module_path}: {e}")
        import traceback
        logger.debug(traceback.format_exc())


async def _register_external_module_nodes(module_path: str, engine: AIOEngine):
    """
    Lazily register nodes from an external module using importlib.
    This works for any installed Python package, not just local files.
    """
    try:
        logger.debug(f"Attempting external module import for nodes: {module_path}")

        # Try to import the module
        module = importlib.import_module(module_path)

        # Get all functions from the module
        functions = inspect.getmembers(module, inspect.isfunction)

        # Filter for functions defined in this module only
        functions = [(func_name, func) for func_name, func in functions
                     if func.__module__ == module.__name__]

        for func_name, func in functions:
            # Check if function has the @node decorator
            if not hasattr(func, '_is_node') or not getattr(func, '_is_node', False):
                continue

            logger.info(f"Registering external node: {func_name} from {module_path}")

            # Create function mapping using full module path
            function_mapping = f"{module_path}.{func_name}"

            # Check if already exists
            existing_model = await engine.find_one(NodeModel, NodeModel.name == func_name)
            if existing_model:
                if existing_model.function_mapping == function_mapping:
                    logger.debug(f"External node {func_name} already registered")
                    continue
                else:
                    # Different mapping, delete and re-create
                    await engine.delete(existing_model)

            # Get node metadata (simplified version, no complex parameter extraction)
            from simstack.models import Parameters
            node_name = getattr(func, '_node_name', func_name)
            node_description = getattr(func, '_node_description', func.__doc__ or "")

            # Create basic NodeModel (no pickle for external modules)
            node_model = NodeModel(
                name=node_name,
                function_mapping=function_mapping,
                description=node_description,
                input_mappings=[],  # Simplified for external modules
                default_parameters=Parameters(),
                pickle_function=None,  # No pickle for external modules
                favorite=False
            )

            await engine.save(node_model)
            logger.debug(f"Successfully registered external node: {func_name}")

    except ImportError as e:
        logger.debug(f"Could not import external module {module_path}: {e}")
    except Exception as e:
        logger.debug(f"Error registering nodes from external module {module_path}: {e}")


async def _lazy_init_all_registered_paths(engine: AIOEngine, models_only: bool = False):
    """
    Lazily initialize all registered paths when we need to find something by name only.
    This is a fallback that scans all paths when we don't have module information.

    Args:
        engine: Database engine
        models_only: If True, only initialize models, not nodes
    """
    from simstack.core.model_table import make_models_for_path
    from simstack.core.node_table import make_nodes_for_path

    logger.debug(f"Lazy initializing all registered paths (models_only={models_only})")

    for path_name in context.path_manager.paths.keys():
        try:
            # Always initialize models
            await make_models_for_path(path_name, context.path_manager, engine)

            # Initialize nodes if not models_only
            if not models_only:
                await make_nodes_for_path(path_name, context.path_manager, engine)

            logger.debug(f"Completed lazy initialization for path: {path_name}")
        except Exception as e:
            logger.debug(f"Error during lazy initialization of path {path_name}: {e}")
            continue


async def _ensure_module_models_registered(module_path: str, engine: AIOEngine):
    """
    Lazily register models from a specific module if they don't exist in the database.
    """
    from pathlib import Path
    from simstack.core.model_table import create_model_models_from_file
    from simstack.util.project_root_finder import find_project_root

    try:
        logger.debug(f"Attempting lazy registration for module: {module_path}")
        # Find the file path for this module
        root_dir = find_project_root()
        module_file_path = None

        # Convert module path to file path
        module_parts = module_path.split('.')
        logger.debug(f"Module parts: {module_parts}")

        for path_name in context.path_manager.paths.keys():
            path_info = context.path_manager.get_path(path_name)
            base_path = Path(path_info["path"])
            drops = path_info["drops"]
            logger.debug(f"Checking path {path_name}: {base_path}, drops: {drops}")

            # Simple path matching: if base_path ends with a package from module_parts,
            # use the remaining parts as the relative path
            adjusted_parts = module_parts[:]

            # For simstack.models.parameters and base /path/to/src/simstack/models
            # we want just 'parameters'
            if 'models' in str(base_path) and len(module_parts) >= 3:
                if module_parts[0] == 'simstack' and module_parts[1] == 'models':
                    adjusted_parts = module_parts[2:]  # Take everything after 'simstack.models'
            elif 'methods' in str(base_path) and len(module_parts) >= 3:
                if module_parts[0] == 'simstack' and module_parts[1] == 'methods':
                    adjusted_parts = module_parts[2:]  # Take everything after 'simstack.methods'

            logger.debug(f"Adjusted module parts for path {path_name}: {adjusted_parts}")

            # Try to find the module file in this path
            # Build the path step by step to handle nested modules correctly
            potential_file = base_path
            for part in adjusted_parts:
                potential_file = potential_file / part
            potential_file = potential_file.with_suffix('.py')
            logger.debug(f"Checking potential file: {potential_file}")

            if potential_file.exists():
                module_file_path = str(potential_file)
                use_pickle = path_info.get("use_pickle", False)
                logger.debug(f"Found file: {module_file_path}")
                break
            else:
                logger.debug(f"File does not exist: {potential_file}")

        if module_file_path:
            logger.info(f"Lazily registering models from {module_file_path}")
            await create_model_models_from_file(module_file_path, engine, drops, use_pickle)
        else:
            logger.debug(f"No file found for module {module_path} in registered paths, trying external import")
            # Try to register from external module using importlib
            await _register_external_module_models(module_path, engine)

    except Exception as e:
        logger.debug(f"Could not lazily register models for module {module_path}: {e}")


async def _ensure_module_nodes_registered(module_path: str, engine: AIOEngine):
    """
    Lazily register nodes from a specific module if they don't exist in the database.
    """
    from pathlib import Path
    from simstack.core.node_table import create_node_models_from_file
    from simstack.util.project_root_finder import find_project_root

    try:
        # Find the file path for this module
        root_dir = find_project_root()
        module_file_path = None

        # Convert module path to file path
        module_parts = module_path.split('.')
        for path_name in context.path_manager.paths.keys():
            path_info = context.path_manager.get_path(path_name)
            base_path = Path(path_info["path"])
            drops = path_info["drops"]

            # Simple path matching: if base_path ends with a package from module_parts,
            # use the remaining parts as the relative path
            adjusted_parts = module_parts[:]

            # For simstack.models.parameters and base /path/to/src/simstack/models
            # we want just 'parameters'
            if 'models' in str(base_path) and len(module_parts) >= 3:
                if module_parts[0] == 'simstack' and module_parts[1] == 'models':
                    adjusted_parts = module_parts[2:]  # Take everything after 'simstack.models'
            elif 'methods' in str(base_path) and len(module_parts) >= 3:
                if module_parts[0] == 'simstack' and module_parts[1] == 'methods':
                    adjusted_parts = module_parts[2:]  # Take everything after 'simstack.methods'

            # Try to find the module file in this path
            # Build the path step by step to handle nested modules correctly
            potential_file = base_path
            for part in adjusted_parts:
                potential_file = potential_file / part
            potential_file = potential_file.with_suffix('.py')

            if potential_file.exists():
                module_file_path = str(potential_file)
                use_pickle = path_info.get("use_pickle", False)
                break

        if module_file_path:
            logger.info(f"Lazily registering nodes from {module_file_path}")
            await create_node_models_from_file(module_file_path, engine, drops, use_pickle)
        else:
            logger.debug(f"No file found for module {module_path} in registered paths, trying external import")
            # Try to register from external module using importlib
            await _register_external_module_nodes(module_path, engine)

    except Exception as e:
        logger.debug(f"Could not lazily register nodes for module {module_path}: {e}")


async def function_from_model(model, task_id: ObjectId) -> Optional[Callable]:
    function_path = model.function_mapping
    module_path, function_name = function_path.rsplit('.', 1)

    # if model.pickle_function is not None:
    #     logger.info(f"task_id: {task_id} found pickle_function for {function_path}")
    #     # The pickle_function is a reference to the FunctionPickle
    #     function_pickle = model.pickle_function
    #     logger.info(f"task_id: {task_id} loading function {function_path} from database")
    #     # Load the function from the FunctionPickle
    #     func = function_pickle.load_function()
    #     logger.info(f"task_id: {task_id} Signature: {inspect.signature(func)} _inner: {hasattr(func, "_inner")}")
    #
    #     # Safe source code retrieval for pickled functions
    #     try:
    #         source_code = inspect.getsource(func)
    #         logger.info(f"Source: {source_code}")
    #     except (OSError, TypeError):
    #         logger.info("Source code not available (function loaded from pickle)")
    #
    #     return func
    # else:

    logger.info(f"task_id: {task_id} loading function {function_path} using regular import")
    # Import the module
    module = importlib.import_module(module_path)
    # Get the function from the module
    function = getattr(module, function_name)
    return function


async def import_function(function_path: str, task_id: ObjectId = None) -> Optional[Callable]:
    """
    Dynamically import a function from a module using its full path.
    load the function information using NodeModel
    load the pickled version if it exists
    if there is no pickled version, use regular import.

    Args:
        function_path: Dot notation path to the function (e.g. 'methods.submodule.function_name')
        task_id: Optional task Id

    Returns:
        The imported function object or None if import fails
    """
    engine = current_engine_context.get()

    node_model = await engine.find_one(NodeModel, NodeModel.function_mapping == function_path)
    if node_model is None:
        # Try lazy initialization for this specific module
        module_path = function_path.rsplit('.', 1)[0]
        await _ensure_module_nodes_registered(module_path, engine)

        # Try again after lazy initialization
        node_model = await engine.find_one(NodeModel, NodeModel.function_mapping == function_path)
        if node_model is None:
            raise LookupError(f"task_id: {task_id} Function {function_path} not found in the NodeModel Table")

    return await function_from_model(node_model, task_id)


async def import_function_by_name(function_name: str, task_id: ObjectId, engine: AIOEngine = None) -> Optional[
    Callable]:
    if not engine:
        engine = context.db.engine

    node_model = await engine.find_one(NodeModel, NodeModel.name == function_name)
    if node_model is None:
        # Try lazy initialization by scanning all registered paths
        logger.debug(f"NodeModel for {function_name} not found, trying lazy initialization")
        await _lazy_init_all_registered_paths(engine, models_only=False)

        # Try to find it again after lazy initialization
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
        module_path, class_name = class_path.rsplit('.', 1)
        model_mapping = await engine.find_one(ModelMapping, ModelMapping.name == class_name)

        # If not found by name, try by mapping
        if not model_mapping:
            model_mapping = await engine.find_one(ModelMapping, ModelMapping.mapping == class_path)
        else:  # when searching by name the path may have changed
            module_path, class_name = model_mapping.mapping.rsplit('.', 1)

        if not model_mapping:
            # Try lazy initialization for this specific module
            logger.info(f"Model not found for {class_name}, attempting lazy initialization for module {module_path}")
            await _ensure_module_models_registered(module_path, engine)

            # Try again after lazy initialization
            model_mapping = await engine.find_one(ModelMapping, ModelMapping.name == class_name)
            if not model_mapping:
                model_mapping = await engine.find_one(ModelMapping, ModelMapping.mapping == class_path)

        if not model_mapping:
            logger.error(f"Error finding ModelMapping for {class_name}")
            raise LookupError(f"Error finding ModelMapping for {class_name}")

        # TODO where do picke classes come from?
        # If we found a ModelMapping with a pickle_class reference, try to load from the database
        # if model_mapping.pickle_class:
        #     logger.info(f"Found ModelMapping for {class_name} with pickle_class")
        #     try:
        #         # The pickle_class is a reference to the ClassPickle
        #         class_pickle = model_mapping.pickle_class
        #         if class_pickle:
        #             logger.info(f"Loading class {class_name} from database")
        #             # Load the class from the ClassPickle
        #             pickle_result = cast(Type[Model], class_pickle.load_class())
        #             return pickle_result
        #         else:
        #             logger.warning(f"ClassPickle not found for {class_name}")
        #             raise LookupError(f"ClassPickle not found for {class_name}")
        #     except Exception as e:
        #         logger.error(f"Error loading class {class_name} from database: {e}")
        #         raise e
        # else:

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
        # Try lazy initialization by scanning all registered paths
        logger.debug(f"ModelMapping for {class_name} not found, trying lazy initialization")
        await _lazy_init_all_registered_paths(engine, models_only=True)

        # Try to find it again after lazy initialization
        model_mapping = await engine.find_one(ModelMapping, ModelMapping.name == class_name)
        if not model_mapping:
            logger.error(f"Error finding ModelMapping for {class_name}")
            raise LookupError(f"Error finding ModelMapping for {class_name}")

    return await import_class(model_mapping.mapping)
