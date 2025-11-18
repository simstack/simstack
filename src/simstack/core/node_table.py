import inspect
import importlib.util
import os
import sys
import asyncio
from pathlib import Path
from typing import Dict, List, Any, Callable, Optional, get_type_hints
import re
from simstack.models import NodeModel, Parameters
from simstack.util.project_root_finder import find_project_root
from simstack.models import ModelMapping
from simstack.models.pickle_models import FunctionPickle
from simstack.core.find_simstack_modules import find_simstack_modules
import logging


logger = logging.getLogger("NodeTable")

def import_module_from_file(file_path: Path):
    """
    Import a Python file as a module.

    Ar
        file_path: Path object pointing to the Python file to import

    Returns:
        Imported module or None if import failed
    """
    try:
        logger.debug(f"Attempting to import module from: {file_path}")
        if not file_path.exists():
            print(f"File not found: {file_path}")
            return []

        root_dir = find_project_root()
        relative_path = file_path.relative_to(root_dir)
        directory, filename = os.path.split(str(relative_path))
        basename = filename.split('.')[0]

        module_name = '.'.join(relative_path.parts[:-1]) + "." + basename

        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
            logger.debug(f"Added {root_dir} to sys.path")
        # Try simple import first
        try:
            module = importlib.import_module(module_name)
            return module
        except ImportError as e:
            logger.error(f"Direct import failed: {e}")

            # Fall back to spec-based import
            spec = importlib.util.spec_from_file_location(module_name, str(file_path))
            if spec is None or spec.loader is None:
                print(f"Failed to create spec for {file_path}")
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
                return module
            except Exception as e:
                logger.error(f"Error processing module: {file_path}  {e}")
                return None

    except Exception as e:
        logger.error(f"Error importing module from {file_path}: {e}")
        import traceback
        traceback.print_exc()
        return None


def is_node_function(func: Callable) -> bool:
    """Check if a function is marked as a node using the @node decorator."""
    return hasattr(func, '_is_node') and getattr(func, '_is_node', False) is True


def parse_docstring(docstring: Optional[str]) -> Dict[str, Any]:
    """Parse docstring to extract description, parameters, and return values."""
    if not docstring:
        return {"description": "", "params": {}, "returns": {}}

    # Clean up docstring
    docstring = inspect.cleandoc(docstring)

    # Extract main description (before any parameters)
    description_match = re.search(r'^(.*?)(?:Args:|Parameters:|Returns:|$)', docstring, re.DOTALL)
    description = description_match.group(1).strip() if description_match else ""

    # Extract parameters
    params = {}
    param_section = re.search(r'(?:Args:|Parameters:)(.*?)(?:Returns:|$)', docstring, re.DOTALL)
    if param_section:
        param_text = param_section.group(1)
        param_matches = re.finditer(r'(\w+)\s*(?:\(([^)]+)\))?\s*:\s*(.+?)(?=\n\s*\w+\s*:|$)', param_text, re.DOTALL)
        for match in param_matches:
            param_name = match.group(1)
            param_type = match.group(2)  # May be None
            param_desc = match.group(3).strip()
            params[param_name] = {
                "type": param_type,
                "description": param_desc
            }

    # Extract return information
    returns = {}
    return_section = re.search(r'Returns:(.*?)$', docstring, re.DOTALL)
    if return_section:
        return_text = return_section.group(1).strip()
        returns["description"] = return_text

    return {
        "description": description,
        "params": params,
        "returns": returns
    }


async def create_node_models_from_file(file_path: str, engine, drops: str, use_pickle: bool = False) -> List[NodeModel]:
    """Create NodeModels for functions decorated with @node in the specified Python file."""

    module = import_module_from_file(Path(file_path))
    if not module:
        return []
    # if hasattr(module, 'context') and not module.context.initialized:
    #     module.context.initialize()
    return await create_node_models_from_module(module, engine, drops, use_pickle)

async def create_node_models_from_module(module: Any, engine, drops: str, use_pickle: bool = False) -> List[
        NodeModel]:

    node_models = []

    functions = inspect.getmembers(module, inspect.isfunction)

    # Filter for functions defined in this module only
    module_name = module.__name__
    functions = [(func_name, func) for func_name, func in functions
                 if func.__module__ == module_name]

    for func_name, func in functions:
        # Skip functions without the @node decorator
        if not is_node_function(func):
            continue

        # Get function signature
        sig = inspect.signature(func)

        # Parse docstring
        docstring_info = parse_docstring(inspect.getdoc(func))

        # Get type hints
        type_hints = get_type_hints(func)

        # Create inputs list from parameters
        inputs = []
        for param_name, param in sig.parameters.items():
            if param_name == 'self':  # Skip self parameter for methods
                continue

            param_info = {
                "name": param_name,
                "type": type_hints.get(param_name, param.annotation.__name__),
                "type_str": str(type_hints.get(param_name, param.annotation.__name__
                if param.annotation != inspect.Parameter.empty
                else "Any"))
            }

            # Add description from docstring if available
            if param_name in docstring_info["params"]:
                param_info["description"] = docstring_info["params"][param_name]["description"]

            # Add default value if available
            if param.default != inspect.Parameter.empty:
                param_info["default"] = param.default

            inputs.append(param_info)

        # Create output information
        outputs = []
        return_type = type_hints.get('return', None)
        if return_type and return_type != type(None):  # Check for actual return type
            output_info = {
                "name": "result",
                "type_str": str(return_type),
                "type": return_type
            }

            # Add description from docstring if available
            if "returns" in docstring_info and docstring_info["returns"]:
                output_info["description"] = docstring_info["returns"]["description"]

            outputs.append(output_info)

        # Create default parameters - ensure it's never None
        parameters = Parameters()
        # First check if the parameters are stored as an attribute
        if hasattr(func, '_node_parameters'):
            parameters = func._node_parameters
        # Otherwise, try to find them in closures
        else:
            closures = inspect.getclosurevars(func)
            for name, values in closures._asdict().items():
                if name == 'nonlocals':
                    continue
                if isinstance(values, dict):
                    kwargs_node = values.get('kwargs_node', None)
                    if kwargs_node and 'parameters' in kwargs_node:
                        parameters = kwargs_node['parameters']
                        break

        # # Verify parameters is a valid Parameters object
        # if not isinstance(parameters, Parameters):
        #     if parameters is None:
        #         # Use empty Parameters if None
        #         parameters = Parameters()
        #     elif hasattr(parameters, '__dict__'):
        #         # Try to convert to Parameters
        #         try:
        #             parameters = Parameters(**parameters.__dict__)
        #         except Exception as e:
        #             logger.error(f"Failed to convert parameters to Parameters object: {e}")
        #             parameters = Parameters()
        #     else:
        #         logger.warning(f"Invalid parameters type: {type(parameters)}. Using default.")
        #         parameters = Parameters()

        # Use node-specific metadata if available
        node_name = getattr(func, '_node_name', func_name)
        node_description = getattr(func, '_node_description', docstring_info["description"])

        # Check if there are valid inputs
        if not inputs:
            logger.warning(f"{node_name} has no inputs.")

        # Safely get input mapping
        input_mappings = []
        if inputs:
            try:
                for specific_input in inputs:
                    if specific_input.get("type") and hasattr(specific_input["type"], "__module__") and hasattr(specific_input["type"], "__name__"):
                        input_mapping = specific_input["type"].__module__ + "." + specific_input["type"].__name__
                        if drops and input_mapping.startswith(drops + "."):
                            input_mapping = input_mapping[len(drops) + 1:]
                        input_mapping_found = await engine.find_one(ModelMapping, ModelMapping.mapping == input_mapping)
                        if not input_mapping_found and input_mapping:
                            logger.error(f"Processing node: {node_name} model {input_mapping} not found in db!")
                        input_mappings.append(input_mapping)
            except Exception as e:
                logger.error(f"Error getting input mapping: {e}")

        function_mapping = module.__name__ + '.' + func_name

        try:
            existing_model = await engine.find_one(NodeModel, NodeModel.name == node_name)
            existing_favorite = False  # Default value if no existing model
            
            if existing_model:
                if function_mapping != existing_model.function_mapping:
                    logger.error(f"Processing module {module.__name__} NodeModel {node_name} already exists in the database\n" +
                                f"                                           DB  Mapping: {existing_model.function_mapping}\n" +
                                f"                                           New Mapping: {function_mapping} skipping.")
                    continue

                # Capture the favorite flag from the existing model
                existing_favorite = getattr(existing_model, 'favorite', False)

                # If it has a pickle_function, delete the corresponding FunctionPickle
                if existing_model.pickle_function:
                    try:
                        # Delete the FunctionPickle directly using the reference
                        await engine.delete(existing_model.pickle_function)
                        logger.debug(f"Deleted FunctionPickle for {node_name}")
                    except Exception as e:
                        logger.error(f"Error deleting FunctionPickle for {node_name}: {e}")

                # Delete the NodeModel entry
                await engine.delete(existing_model)
                logger.debug(f"Deleted NodeModel entry for {node_name}")

            # Create a FunctionPickle instance only if use_pickle is true
            function_pickle = None
            if use_pickle:
                try:
                    # Create a FunctionPickle instance
                    function_pickle = FunctionPickle(
                        name=func_name,
                        module_path=func.__module__
                    )

                    # Store the function
                    function_pickle.store_function(func)

                    # Save the FunctionPickle instance
                    function_pickle = await engine.save(function_pickle)

                    logger.debug(f"Created FunctionPickle for {node_name}")
                except Exception as e:
                    logger.error(f"Error creating FunctionPickle for {node_name}: {e}")
                    function_pickle = None

            # Create and save the node model
            node_model = NodeModel(
                name=node_name,
                function_mapping=function_mapping,
                description=node_description,
                input_mappings=input_mappings,
                default_parameters=parameters,
                pickle_function=function_pickle,
                favorite=existing_favorite  # Set the favorite flag from the existing model
            )

            logger.debug(f"NodeModel: {node_model.name}, {node_model.function_mapping}, {node_model.input_mappings}")
            await engine.save(node_model)
            node_models.append(node_model)
        except Exception as e:
            logger.error(f"Error creating/saving NodeModel {node_name}: {e}")
            import traceback
            traceback.print_exc()

    return node_models


async def make_node_table(engine):
    from simstack.core.context import context
    # Iterate over all paths in the PathManager

    all_modules = find_simstack_modules()
    for module in all_modules:
        logger.info(f"Processing module: {module}")
        module = importlib.import_module(module)
        await create_node_models_from_module(module, engine, '')

    for path_name in context.path_manager.paths.keys():
        await make_nodes_for_path(path_name,  context.path_manager, engine)

async def make_nodes_for_path(path_name, path_manager, engine):
    path_info = path_manager.get_path(path_name)
    path = path_info["path"]
    drops = path_info["drops"]
    use_pickle = path_info.get("use_pickle", False)
    logger.info(f"Making node_registry entries from {path}")
    for file_path in path_manager.find_python_files(path_name):
        # Create node models from the file
        node_models = await create_node_models_from_file(file_path, engine, drops, use_pickle)
        logger.debug(f"Created {len(node_models)} node models from {file_path}")


def create_node_table_main():
    # Don't create a new loop with asyncio.run, use an existing one
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    from simstack.core.context import context
    # Initialize context with this loop
    context.initialize()

    # Run in the same loop
    loop.run_until_complete(make_node_table(context.db.engine))
    loop.close()

if __name__ == "__main__":
    create_node_table_main()
