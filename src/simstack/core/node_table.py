import argparse
import asyncio
import inspect
import logging
import re
from pathlib import Path
from typing import Callable, List, Optional, get_type_hints, Dict, Any

from simstack.core.context import context
from simstack.core.find_simstack_modules import find_simstack_modules
from simstack.models import Parameters
from simstack.models.models import NodeModel, ModelMapping
from simstack.util.import_module import import_module_from_file

logger = logging.getLogger("NodeTable")


def is_node_function(func: Callable) -> bool:
    """Check if a function is marked as a node using the @node decorator."""
    return hasattr(func, "_is_node") and getattr(func, "_is_node", False) is True


def parse_docstring(docstring: Optional[str]) -> Dict[str, Any]:
    """Parse docstring to extract description, parameters, and return values."""
    if not docstring:
        return {"description": "", "params": {}, "returns": {}}

    # Clean up docstring
    docstring = inspect.cleandoc(docstring)

    # Extract the main description (before any parameters)
    description_match = re.search(
        r"^(.*?)(?:Args:|Parameters:|Returns:|$)", docstring, re.DOTALL
    )
    description = description_match.group(1).strip() if description_match else ""

    # Extract parameters
    params = {}
    param_section = re.search(
        r"(?:Args:|Parameters:)(.*?)(?:Returns:|$)", docstring, re.DOTALL
    )
    if param_section:
        param_text = param_section.group(1)
        param_matches = re.finditer(
            r"(\w+)\s*(?:\(([^)]+)\))?\s*:\s*(.+?)(?=\n\s*\w+\s*:|$)",
            param_text,
            re.DOTALL,
        )
        for match in param_matches:
            param_name = match.group(1)
            param_type = match.group(2)  # May be None
            param_desc = match.group(3).strip()
            params[param_name] = {"type": param_type, "description": param_desc}

    # Extract return information
    returns = {}
    return_section = re.search(r"Returns:(.*?)$", docstring, re.DOTALL)
    if return_section:
        return_text = return_section.group(1).strip()
        returns["description"] = return_text

    return {"description": description, "params": params, "returns": returns}


class CreateNodeTable:
    """
    Helper class to build the node table without passing around many parameters.

    Usage:
        creator = CreateNodeTable(engine)
        await creator.make_node_table()
    """

    def __init__(self, engine):
        # Ensure context is initialized and store frequently used objects
        if not context.initialized:
            context.initialize()

        self.context = context
        self.engine = engine
        self.path_manager = context.path_manager

    async def make_node_table(self):
        """Entry point to (re)build the node table."""
        # First, process all simstack modules
        all_modules = find_simstack_modules()
        for module_name in all_modules:
            logger.info(f"Processing node module: {module_name}")
            try:
                # We import modules lazily via the path manager where possible,
                # but for discovered package modules we can use standard import.
                module = __import__(module_name, fromlist=["*"])
            except Exception as exc:
                logger.warning("Failed to import module %s: %s", module_name, exc)
                continue

            await self._register_nodes_from_module(module, drops="")

        # Then process all configured paths (user/project code)
        for path_name in self.path_manager.paths.keys():
            await self._make_nodes_for_path(path_name)

    async def _make_nodes_for_path(self, path_name: str):
        """Discover/register nodes for all Python files under a configured path."""
        path_info = self.path_manager.get_path(path_name)
        base_path = path_info["path"]
        drops = path_info["drops"]

        logger.info(f"Making node_table entries for files in {base_path}")

        for file_path in self.path_manager.find_python_files(path_name):
            await self._create_nodes_from_file(file_path, drops)

    async def _create_nodes_from_file(self, file_path: str, drops: str):
        """Create node entries for function definitions in the specified Python file."""
        logger.debug(f"Processing nodes from: {file_path}")
        module = import_module_from_file(Path(file_path))
        if not module:
            logger.debug("Skipping %s because module import returned None", file_path)
            return

        await self._register_nodes_from_module(module, drops)

    async def _register_nodes_from_module(self, module, drops: str):
        """
        Core logic to discover node functions in a module and (re)create NodeModel entries.

        Heuristic:
        - All top-level callables (functions) whose names do not start with '_'
        - Only functions actually defined in this module.
        """
        functions: List[tuple[str, Callable]] = inspect.getmembers(
            module, inspect.isfunction
        )
        # Filter for functions defined in this module only
        module_name = module.__name__
        functions = [
            (func_name, func)
            for func_name, func in functions
            if func.__module__ == module_name
        ]

        for func_name, func in functions:
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
                if param_name == "self":  # Skip self parameter for methods
                    continue

                param_info = {
                    "name": param_name,
                    "type": type_hints.get(param_name, param.annotation.__name__),
                    "type_str": str(
                        type_hints.get(
                            param_name,
                            param.annotation.__name__
                            if param.annotation != inspect.Parameter.empty
                            else "Any",
                        )
                    ),
                }

                # Add description from docstring if available
                if param_name in docstring_info["params"]:
                    param_info["description"] = docstring_info["params"][param_name][
                        "description"
                    ]

                # Add default value if available
                if param.default != inspect.Parameter.empty:
                    param_info["default"] = param.default

                inputs.append(param_info)

            # Create output information
            outputs = []
            return_type = type_hints.get("return", None)
            if return_type and return_type != type(
                None
            ):  # Check for actual return type
                output_info = {
                    "name": "result",
                    "type_str": str(return_type),
                    "type": return_type,
                }

                # Add description from docstring if available
                if "returns" in docstring_info and docstring_info["returns"]:
                    output_info["description"] = docstring_info["returns"][
                        "description"
                    ]

                outputs.append(output_info)

            # Create default parameters - ensure it's never None
            parameters = Parameters()
            # First check if the parameters are stored as an attribute
            if hasattr(func, "_node_parameters"):
                parameters = func._node_parameters
            # Otherwise, try to find them in closures
            else:
                closures = inspect.getclosurevars(func)
                for name, values in closures._asdict().items():
                    if name == "nonlocals":
                        continue
                    if isinstance(values, dict):
                        kwargs_node = values.get("kwargs_node", None)
                        if kwargs_node and "parameters" in kwargs_node:
                            parameters = kwargs_node["parameters"]
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
            node_name = getattr(func, "_node_name", func_name)
            node_description = getattr(
                func, "_node_description", docstring_info["description"]
            )

            # Check if there are valid inputs
            if not inputs:
                logger.warning(f"{node_name} has no inputs.")

            # Safely get input mapping
            input_mappings = []
            if inputs:
                try:
                    for specific_input in inputs:
                        if (
                            specific_input.get("type")
                            and hasattr(specific_input["type"], "__module__")
                            and hasattr(specific_input["type"], "__name__")
                        ):
                            input_mapping = (
                                specific_input["type"].__module__
                                + "."
                                + specific_input["type"].__name__
                            )
                            if drops and input_mapping.startswith(drops + "."):
                                input_mapping = input_mapping[len(drops) + 1 :]
                            input_mapping_found = await self.engine.find_one(
                                ModelMapping, ModelMapping.mapping == input_mapping
                            )
                            if not input_mapping_found and input_mapping:
                                logger.error(
                                    f"Processing node: {node_name} model {input_mapping} not found in db!"
                                )
                            input_mappings.append(input_mapping)
                except Exception as e:
                    logger.error(f"Error getting input mapping: {e}")

            function_mapping = module.__name__ + "." + func_name

            try:
                existing_model = await self.engine.find_one(
                    NodeModel, NodeModel.name == node_name
                )
                existing_favorite = False  # Default value if no existing model

                if existing_model:
                    if function_mapping != existing_model.function_mapping:
                        logger.error(
                            f"Processing module {module.__name__} NodeModel {node_name} already exists in the database\n"
                            + f"                                           DB  Mapping: {existing_model.function_mapping}\n"
                            + f"                                           New Mapping: {function_mapping} skipping."
                        )
                        continue

                    # Capture the favorite flag from the existing model
                    existing_favorite = getattr(existing_model, "favorite", False)

                    # If it has a pickle_function, delete the corresponding FunctionPickle
                    if existing_model.pickle_function:
                        try:
                            # Delete the FunctionPickle directly using the reference
                            await self.engine.delete(existing_model.pickle_function)
                            logger.debug(f"Deleted FunctionPickle for {node_name}")
                        except Exception as e:
                            logger.error(
                                f"Error deleting FunctionPickle for {node_name}: {e}"
                            )

                    # Delete the NodeModel entry
                    await self.engine.delete(existing_model)
                    logger.debug(f"Deleted NodeModel entry for {node_name}")

                function_pickle = None

                # Create and save the node model
                node_model = NodeModel(
                    name=node_name,
                    function_mapping=function_mapping,
                    description=node_description,
                    input_mappings=input_mappings,
                    default_parameters=parameters,
                    pickle_function=function_pickle,
                    favorite=existing_favorite,  # Set the favorite flag from the existing model
                )

                logger.debug(
                    f"NodeModel: {node_model.name}, {node_model.function_mapping}, {node_model.input_mappings}"
                )
                await self.engine.save(node_model)
                # node_models.append(node_model)
            except Exception as e:
                logger.error(f"Error creating/saving NodeModel {node_name}: {e}")
                import traceback

                traceback.print_exc()


# Public API preserved for existing callers (e.g. tests)
async def make_node_table(engine):
    """
    Rebuild the node table using the given engine.

    This is a thin wrapper around CreateNodeTable for backward compatibility.
    """
    creator = CreateNodeTable(engine)
    await creator.make_node_table()


def create_node_table_main():
    """
    CLI-style entry point to (re)build the node table.

    Uses a dedicated event loop, matching the pattern used for model table creation.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    context.initialize(log_level=level)

    # Set pymongo logger level to INFO
    logging.getLogger("pymongo").setLevel(logging.INFO)

    try:
        loop.run_until_complete(make_node_table(context.db.engine))
    finally:
        loop.close()


if __name__ == "__main__":
    create_node_table_main()
