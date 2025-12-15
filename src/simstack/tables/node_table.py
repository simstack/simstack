import inspect
import logging
import re
from typing import Callable, List, Optional, get_type_hints, Dict, Any

from simstack.models import Parameters
from simstack.models.models import NodeModel, ModelMapping, DataMapping
from simstack.tables.table_builder import TableBuilderBase

logger = logging.getLogger("NodeTable")


def is_node_function(func: Callable) -> bool:
    """Check if a function is marked as a node using the @node decorator."""
    return hasattr(func, "_is_node") and getattr(func, "_is_node", False) is True


def parse_docstring(docstring: Optional[str]) -> Dict[str, Any]:
    """Parse docstring to extract description, parameters, and return values."""
    if not docstring:
        return {"description": "", "params": {}, "returns": {}, "simstack_results": {}}

    # Clean up docstring
    docstring = inspect.cleandoc(docstring)

    # Extract the main description (before any parameters)
    description_match = re.search(
        r"^(.*?)(?:Args:|Parameters:|Returns:|SimstackResult:|$)", docstring, re.DOTALL
    )
    description = description_match.group(1).strip() if description_match else ""

    # Extract parameters
    params = {}
    param_section = re.search(
        r"(?:Args:|Parameters:)(.*?)(?:Returns:|SimstackResult:|$)", docstring, re.DOTALL
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
            param_type = match.group(2)  # Maybe None
            param_desc = match.group(3).strip()
            params[param_name] = {"type": param_type, "description": param_desc}

    # Extract return information
    returns = {}
    return_section = re.search(r"Returns:(.*?)(?:SimstackResult:|$)", docstring, re.DOTALL)
    if return_section:
        return_text = return_section.group(1).strip()
        returns["description"] = return_text

    # Extract SimstackResult information
    simstack_results = {}
    simstack_section = re.search(r"SimstackResult:(.*?)$", docstring, re.DOTALL)
    if simstack_section:
        simstack_text = simstack_section.group(1)
        simstack_matches = re.finditer(
            r"(\w+)\s*\(([^)]+)\)\s*(.+?)(?=\n\s*\w+\s*\(|$)",
            simstack_text,
            re.DOTALL,
        )
        for match in simstack_matches:
            result_name = match.group(1)
            result_type = match.group(2).strip()
            result_desc = match.group(3).strip()
            simstack_results[result_name] = {"type": result_type, "description": result_desc}

    return {"description": description, "params": params, "returns": returns, "simstack_results": simstack_results}


class CreateNodeTable(TableBuilderBase):
    """
    Helper class to build the node table without passing around many parameters.

    Usage:
        creator = CreateNodeTable(engine)
        await creator.make_node_table()
    """

    @property
    def logger(self) -> logging.Logger:
        return logger

    async def _process_module(self, module, drops: str) -> None:
        await self._register_nodes_from_module(module, drops)

    def _discover_module_functions(self, module) -> List[tuple[str, Callable]]:
        """
        Return (name, func) for functions that are defined in `module` (not imported).
        """
        functions: List[tuple[str, Callable]] = inspect.getmembers(module, inspect.isfunction)
        module_name = module.__name__
        return [
            (func_name, func)
            for func_name, func in functions
            if func.__module__ == module_name
        ]

    def _build_inputs(
        self,
        sig: inspect.Signature,
        type_hints: Dict[str, Any],
        docstring_info: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        inputs: List[Dict[str, Any]] = []
        for param_name, param in sig.parameters.items():
            if param_name == "self":  # Skip self parameter for methods
                continue

            param_info: Dict[str, Any] = {
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

            if param_name in docstring_info["params"]:
                param_info["description"] = docstring_info["params"][param_name]["description"]

            if param.default != inspect.Parameter.empty:
                param_info["default"] = param.default

            inputs.append(param_info)

        return inputs

    def _build_outputs(
        self,
        type_hints: Dict[str, Any],
        docstring_info: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        outputs: List[Dict[str, Any]] = []
        return_type = type_hints.get("return", None)
        if return_type and return_type != type(None):  # Check for actual return type
            output_info: Dict[str, Any] = {
                "name": "result",
                "type_str": str(return_type),
                "type": return_type,
            }
            if "returns" in docstring_info and docstring_info["returns"]:
                output_info["description"] = docstring_info["returns"]["description"]
            outputs.append(output_info)

        return outputs

    def _extract_default_parameters(self, func: Callable) -> Parameters:
        """
        Best-effort extraction of node Parameters from either a direct attribute
        or from closure variables. Always returns a non-None Parameters().
        """
        parameters = Parameters()

        if hasattr(func, "_node_parameters"):
            return func._node_parameters

        closures = inspect.getclosurevars(func)
        for name, values in closures._asdict().items():
            if name == "nonlocals":
                continue
            if isinstance(values, dict):
                kwargs_node = values.get("kwargs_node", None)
                if kwargs_node and "parameters" in kwargs_node:
                    return kwargs_node["parameters"]

        return parameters

    async def _resolve_input_mappings(
        self,
        node_name: str,
        inputs: List[Dict[str, Any]],
        drops: str,
    ) -> List[str]:
        """
        Convert input python types to ModelMapping.mapping strings and validate their existence.
        """
        input_mappings: List[str] = []
        if not inputs:
            return input_mappings

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

        return input_mappings

    async def _delete_existing_node_model_if_needed(
        self,
        node_name: str,
        function_mapping: str,
    ) -> tuple[bool, bool]:
        """
        Returns:
            (should_continue, existing_favorite)

        If a NodeModel exists with the same name but different function_mapping,
        logs and signals to skip processing.
        """
        existing_model = await self.engine.find_one(NodeModel, NodeModel.name == node_name)
        if not existing_model:
            return False, False

        if function_mapping != existing_model.function_mapping:
            logger.error(
                f"Processing module NodeModel {node_name} already exists in the database\n"
                + f"                                           DB  Mapping: {existing_model.function_mapping}\n"
                + f"                                           New Mapping: {function_mapping} skipping."
            )
            return True, False

        existing_favorite = getattr(existing_model, "favorite", False)

        if existing_model.pickle_function:
            try:
                await self.engine.delete(existing_model.pickle_function)
                #logger.debug(f"Deleted FunctionPickle for {node_name}")
            except Exception as e:
                logger.error(f"Error deleting FunctionPickle for {node_name}: {e}")

        await self.engine.delete(existing_model)
        # logger.debug(f"Deleted NodeModel entry for {node_name}")

        return False, existing_favorite


    async def _register_nodes_from_module(self, module, drops: str):
        """
        Core logic to discover node functions in a module and (re)create NodeModel entries.

        Heuristic:
        - All top-level callables (functions) whose names do not start with '_'
        - Only functions actually defined in this module.
        """
        functions = self._discover_module_functions(module)

        for func_name, func in functions:
            if not is_node_function(func):
                continue

            sig = inspect.signature(func)
            docstring_info = parse_docstring(inspect.getdoc(func))
            type_hints = get_type_hints(func)

            inputs = self._build_inputs(sig, type_hints, docstring_info)
            outputs = self._build_outputs(type_hints, docstring_info)

            parameters = self._extract_default_parameters(func)

            node_name = getattr(func, "_node_name", func_name)
            node_description = getattr(func, "_node_description", docstring_info["description"])

            if not inputs:
                logger.warning(f"{node_name} has no inputs -- this means the node will be executed only once.")

            input_mappings = await self._resolve_input_mappings(node_name, inputs, drops)
            function_mapping = module.__name__ + "." + func_name
            try:
                should_skip, existing_favorite = await self._delete_existing_node_model_if_needed(
                    node_name, function_mapping
                )
                if should_skip:
                    continue

                data_mappings = []
                for data_input, input_mapping in zip(inputs,input_mappings):
                    if data_input.get("type") and hasattr(data_input["type"], "__name__"):
                        data_input_mapping = (
                                data_input["type"].__module__
                                + "."
                                + data_input["type"].__name__
                        )
                        if data_input_mapping != input_mapping:
                           self.logger.error(f"Type mismatch for input '{data_input['name']}': expected '{data_input['type'].__name__}', got '{input_mapping}'")
                    else:
                        self.logger.error(f"No type specified for input '{data_input['name']}'")
                    data_mappings.append(DataMapping(name=data_input['name'], mapping=input_mapping))

                
                node_model = NodeModel(
                    name=node_name,
                    function_mapping=function_mapping,
                    description=node_description,
                    input_mappings=data_mappings,
                    result_mappings=[],
                    called_nodes=[],
                    default_parameters=parameters,
                    pickle_function=None,
                    favorite=existing_favorite,
                )

                logger.info(
                    f"NodeModel: {node_model.name}, {node_model.function_mapping}, {node_model.input_mappings}"
                )
                await self.engine.save(node_model)

            except Exception as e:
                logger.error(f"Error creating/saving NodeModel {node_name}: {e}")
                import traceback

                traceback.print_exc()


async def make_node_table(engine, dirs: list[str] = None, drops: str = None, write_schema: bool = False):
    """
    Rebuild the node table using the given engine.

    This is a thin wrapper around CreateNodeTable for backward compatibility.
    """
    creator = CreateNodeTable(engine, write_schema=write_schema)
    await creator.build(dirs=dirs, drops=drops)


def create_node_table_main():
    """
    CLI-style entry point to (re)build the node table.

    Uses a dedicated event loop, matching the pattern used for model table creation.
    """
    TableBuilderBase.cli_main(CreateNodeTable)


if __name__ == "__main__":
    create_node_table_main()
