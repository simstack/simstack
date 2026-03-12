import inspect
import logging
import re
import types
from typing import Callable, List, get_type_hints, Dict, Any, Type, get_origin, get_args

from docutils.nodes import description
from odmantic.query import desc

from simstack.core.simstack_result import SimstackResult
from simstack.tables.node_children import update_node_children
from simstack.models import Parameters
from simstack.models.models import NodeModel, ModelMapping, DataMapping
from simstack.tables.table_builder import TableBuilderBase
from simstack.util.docstring_parser import DocstringParser
from simstack.util.importer import import_class_by_name

logger = logging.getLogger("NodeTable")


def is_node_function(func: Callable) -> bool:
    """Check if a function is marked as a node using the @node decorator."""
    return hasattr(func, "_is_node") and getattr(func, "_is_node", False) is True


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
        doc_params: Dict[str, Any] | None,
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

            if doc_params and param_name in doc_params:
                param_info["description"] = doc_params[param_name].get("description")

            if param.default != inspect.Parameter.empty:
                param_info["default"] = param.default

            inputs.append(param_info)

        return inputs

    def _parse_generic_type(self, type_str: str) -> tuple[str, str | None]:
        """
        Parse generic types like List[X] or Dict[str,X] and extract the inner type.
        Returns (wrapper, inner_type) where wrapper is 'List' or 'Dict' and inner_type is the model type.
        """
        list_match = re.match(r'List\[(.*?)\]', type_str)
        if list_match:
            return 'List', list_match.group(1)

        dict_match = re.match(r'Dict\[str,\s*(.*?)\]', type_str)
        if dict_match:
            return 'Dict', dict_match.group(1)

        return None, None

    async def _build_outputs(
            self,
            func_name: str,
            type_hints: Dict[str, Any],
            parser: DocstringParser,
            drops: str
    ) -> List[DataMapping]:

        outputs: List[Dict[str, Any]] = []
        return_type = type_hints.get("return", None)
        doc_returns = parser.returns()
        if return_type and return_type != type(None):  # Check for actual return type
            output_info: Dict[str, Any] = {
                "name": "result",
                "type_str": str(return_type),
                "type": return_type,
            }
            if doc_returns:
                output_info["description"] = doc_returns.get("description")
            outputs.append(output_info)

        returns_simstack_result = any(output["type"] == SimstackResult for output in outputs)
        result_mappings = []
        if returns_simstack_result:
            if len(outputs) > 1:
                logger.warning(f"Node {func_name} returns more than one output, one of which is SimstackResult")
            else:
                doc_simstack_result = parser.simstack_results()
                if doc_simstack_result is None:
                    logger.warning(f"The docstring of {func_name} does not defines its SimstackResult outputs")
                else:
                    for name, data in doc_simstack_result.items():
                        output_mapping = None
                        try:
                            wrapper, inner_type_str = self._parse_generic_type(data["type"])
                            if wrapper:
                                # Handle List[type] or Dict[str,type]
                                inner_mapping = None
                                if "." in inner_type_str:
                                    inner_mapping = inner_type_str
                                else:
                                    try:
                                        inner_model = await import_class_by_name(inner_type_str)
                                    except (ValueError, LookupError):
                                        inner_model = None
                                    if inner_model is not None:
                                        inner_mapping = self.get_class_mapping(inner_model, drops)
                                if inner_mapping is not None:
                                    if wrapper == 'List':
                                        output_mapping = f"List[{inner_mapping}]"
                                    else:  # Dict
                                        output_mapping = f"Dict[str,{inner_mapping}]"
                            elif "." in data["type"]:
                                # It's a full mapping path
                                output_mapping = data["type"]
                            else:
                                # It's a single class name
                                try:
                                    output_model = await import_class_by_name(data["type"])
                                    output_mapping = self.get_class_mapping(output_model, drops)
                                except (ValueError, LookupError) as e:
                                    logger.error(f"Could not parse '{data['type']}' to mapping: {e}")
                                    output_mapping = None
                            if output_mapping is not None:
                                result_mappings.append(DataMapping(name=name, mapping=output_mapping, description=data.get("description")))
                        except (ValueError, LookupError) as e:
                            logger.error(f"Could not parse '{data['type']}' to mapping: {e}")
        else:  # not a SimstackResult
            for output in outputs:
                try:
                    output_mapping = self.get_class_mapping(output["type"], drops)
                    result_mappings.append(DataMapping(name=output["name"], mapping=output_mapping, description=output.get("description")))
                except ValueError:
                    logger.error(f"Could not parse '{output['type']}' to mapping")
        return result_mappings


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

    async def _delete_existing_node_model_if_needed(
            self,
            node_name: str,
            function_mapping: str,
    ) -> tuple[bool, bool]:
        """
        If a NodeModel with the same name exists, delete it (and its pickle if present),
        but preserve the 'favorite' flag for the new entry.

        Returns:
            (should_skip, existing_favorite)
        """
        try:
            existing_model = await self.engine.find_one(NodeModel, NodeModel.name == node_name)
        except Exception as e:
            logger.error(f"Error finding existing NodeModel {node_name}: {e}")
            return False, False

        if not existing_model:
            return False, False

        if function_mapping != existing_model.function_mapping:
            logger.error(
                f"NodeModel '{node_name}' already exists in the database\n"
                f"    DB  Mapping: {existing_model.function_mapping}\n"
                f"    New Mapping: {function_mapping}\n"
                f"New Mapping will overwrite DB Mapping."
            )

        existing_favorite = getattr(existing_model, "favorite", False)

        if getattr(existing_model, "pickle_function", None):
            try:
                await self.engine.delete(existing_model.pickle_function)
            except Exception as e:
                logger.error(f"Error deleting FunctionPickle for {node_name}: {e}")

        try:
            await self.engine.delete(existing_model)
        except Exception as e:
            logger.error(f"Error deleting existing NodeModel {node_name}: {e}")

        return False, existing_favorite


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

    def get_class_mapping(self, typ: Type, drops: str = "") -> str:
        """Return the class mapping for a given type, optionally dropping a prefix.

        This is tolerant to Optional / union types such as ``FloatData | None``
        on Python 3.10+ (PEP 604) by unwrapping the union and resolving the
        underlying model type.
        """

        # Handle PEP 604 unions like ``FloatData | None``. In Python 3.12,
        # ``FloatData | None`` is a ``types.UnionType``.
        if isinstance(typ, types.UnionType):
            # ``get_args`` returns the individual union members, e.g.
            # (FloatData, NoneType). We drop ``NoneType`` and keep the
            # underlying model type for mapping purposes.
            args = [t for t in get_args(typ) if t is not type(None)]
            if len(args) == 1:
                # Treat Optional[T] as just T for mapping purposes.
                return self.get_class_mapping(args[0], drops)

        # Handle typing.Optional[T] / typing.Union[T, None]
        origin = get_origin(typ)
        if origin is not None:
            typing_module = __import__("typing")
            if origin is getattr(typing_module, "Union", None):
                args = [t for t in get_args(typ) if t is not type(None)]
                if len(args) == 1:
                    return self.get_class_mapping(args[0], drops)

        # Default behavior: expect a concrete model class
        if hasattr(typ, "__module__") and hasattr(typ, "__name__"):
            mapping = typ.__module__ + "." + typ.__name__
            if drops and mapping.startswith(drops + "."):
                mapping = mapping[len(drops) + 1:]
            return mapping

        raise ValueError(f"Could not parse '{typ}' to mapping")

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


            parser = DocstringParser(inspect.getdoc(func))
            doc_description = parser.description()
            doc_params = parser.params()
            doc_returns = parser.returns()

            type_hints = get_type_hints(func)

            inputs = self._build_inputs(sig, type_hints, doc_params)

            parameters = self._extract_default_parameters(func)
            node_name = getattr(func, "_node_name", func_name)
            node_description = getattr(func, "_node_description", doc_description or "")

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
                        data_input_mapping = self.get_class_mapping(data_input["type"], drops)
                        if data_input_mapping != input_mapping:
                            self.logger.error(f"Type mismatch for input '{data_input['name']}': expected '{data_input['type'].__name__}', got '{input_mapping}'")
                    else:
                        self.logger.error(f"No type specified for input '{data_input['name']}'")
                    data_mappings.append(DataMapping(name=data_input['name'], mapping=input_mapping, description=data_input.get("description")))

                result_mappings = await self._build_outputs(func_name, type_hints, parser, drops)

                node_model = NodeModel(
                    name=node_name,
                    function_mapping=function_mapping,
                    description=node_description,
                    input_mappings=data_mappings,
                    result_mappings=result_mappings,
                    called_nodes=[], # we need to first build the full list, will be filled in second_stage
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

    async def second_stage(self, drops):
        await update_node_children(self.engine, drops)

    async def clear_table(self) -> None:
        self.logger.info("Clearing NodeModel collection")
        await self.engine.get_collection(NodeModel).drop()

async def make_node_table(
    engine,
    dirs: list[str] = None,
    drops: str = None,
    write_schema: bool = False,
    clear: bool = False,
):
    """
    Rebuild the node table using the given engine.

    This is a thin wrapper around CreateNodeTable for backward compatibility.
    """
    creator = CreateNodeTable(engine, write_schema=write_schema)
    await creator.build(dirs=dirs, drops=drops, clear=clear)


def create_node_table_main():
    """
    CLI-style entry point to (re)build the node table.

    Uses a dedicated event loop, matching the pattern used for model table creation.
    """
    TableBuilderBase.cli_main(CreateNodeTable)


if __name__ == "__main__":
    create_node_table_main()