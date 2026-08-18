import inspect
import logging
import re
from pathlib import Path
from typing import Callable, List, get_type_hints, Dict, Any, Type


from simstack.core.simstack_result import SimstackResult
from simstack.core.context import context
from simstack.tables.node_children import update_node_children
from simstack.models import Parameters
from simstack.models.models import NodeModel, ModelMapping, DataMapping
from simstack.tables.table_builder import TableBuilderBase
from simstack.util.db import Database
from simstack.util.docstring_parser import DocstringParser
from simstack.util.importer import import_class_by_name

logger = logging.getLogger("NodeTable")


def is_node_function(func: Callable[..., Any]) -> bool:
    """Check if a function is marked as a node using the @node decorator."""
    return hasattr(func, "_is_node") and getattr(func, "_is_node", False) is True


class CreateNodeTable(TableBuilderBase):
    """
    Helper class to build the node table without passing around many parameters.

    Usage:
        creator = CreateNodeTable(database)
        await creator.make_node_table()
    """

    @property
    def logger(self) -> logging.Logger:
        return logger

    async def build(self, *args, **kwargs) -> None:
        refresh_mappings = kwargs.pop("refresh_mappings", True)
        if not context.initialized:
            await context.initialize()
        if refresh_mappings:
            await context.refresh_mappings(models=True, nodes=False)
        await super().build(*args, **kwargs)
        if refresh_mappings:
            await context.refresh_mappings(models=False, nodes=True)

    async def _process_module(self, module: Any, drops: str) -> None:
        await self._register_nodes_from_module(module, drops)

    def _discover_module_functions(
        self, module: Any
    ) -> List[tuple[str, Callable[..., Any]]]:
        """
        Return (name, func) for functions that are defined in `module` (not imported).
        """
        functions: List[tuple[str, Callable[..., Any]]] = inspect.getmembers(
            module, inspect.isfunction
        )
        module_name = module.__name__
        return [
            (func_name, func)
            for func_name, func in functions
            if func.__module__ == module_name
        ]

    @staticmethod
    def _annotation_type_str(annotation: Any) -> str:
        """Return a display string for a signature annotation without assuming a class."""
        if annotation is inspect.Parameter.empty:
            return "Any"
        if isinstance(annotation, str):
            return annotation
        if hasattr(annotation, "__name__"):
            return annotation.__name__
        return str(annotation)

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
            # *args / **kwargs are runtime plumbing, not node inputs.
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            # Prefer resolved type hints. Do not evaluate param.annotation.__name__
            # as a dict.get() default: with `from __future__ import annotations`,
            # annotations are strings and that attribute access raises, wiping the
            # input entry and leaving NodeModel.input_mappings empty.
            resolved_type = type_hints.get(param_name)
            if resolved_type is None:
                resolved_type = (
                    None
                    if param.annotation is inspect.Parameter.empty
                    else param.annotation
                )
                type_str = self._annotation_type_str(param.annotation)
            else:
                type_str = self._annotation_type_str(resolved_type)

            if resolved_type is None:
                logger.error(f"Could not parse type for {param_name}: missing annotation")
                continue

            param_info: Dict[str, Any] = {
                "name": param_name,
                "type": resolved_type,
                "type_str": type_str,
            }

            if doc_params and param_name in doc_params:
                param_info["description"] = doc_params[param_name].get("description")

            if param.default != inspect.Parameter.empty:
                param_info["default"] = param.default

            inputs.append(param_info)

        return inputs

    def _parse_generic_type(self, type_str: str) -> tuple[str | None, str | None]:
        """
        Parse generic types like List[X] or Dict[str,X] and extract the inner type.
        Returns (wrapper, inner_type) where wrapper is 'List' or 'Dict' and inner_type is the model type.
        """
        list_match = re.match(r"List\[(.*?)\]", type_str)
        if list_match:
            return "List", list_match.group(1)

        dict_match = re.match(r"Dict\[str,\s*(.*?)\]", type_str)
        if dict_match:
            return "Dict", dict_match.group(1)

        return None, None

    @staticmethod
    def _normalize_docstring_type(type_str: str) -> str:
        """Strip top-level docstring qualifiers like ', optional' from a parsed type string."""
        normalized = (type_str or "").strip()
        bracket_depth = 0

        for index, char in enumerate(normalized):
            if char == "[":
                bracket_depth += 1
            elif char == "]" and bracket_depth > 0:
                bracket_depth -= 1
            elif char == "," and bracket_depth == 0:
                return normalized[:index].strip()

        return normalized

    async def _build_outputs(
        self,
        func_name: str,
        type_hints: Dict[str, Any],
        parser: DocstringParser,
        drops: str,
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

        returns_simstack_result = any(
            output["type"] == SimstackResult for output in outputs
        )
        result_mappings = []
        db = self.db
        if returns_simstack_result:
            if len(outputs) > 1:
                logger.warning(
                    f"Node {func_name} returns more than one output, one of which is SimstackResult"
                )
            else:
                doc_simstack_result = parser.simstack_results()
                if doc_simstack_result is None:
                    logger.warning(f"The docstring of {func_name} does not defines its SimstackResult outputs")
                else:
                    for name, data in doc_simstack_result.items():
                        output_mapping = None
                        output_type = self._normalize_docstring_type(data["type"])
                        try:
                            wrapper, inner_type_str = self._parse_generic_type(
                                output_type
                            )
                            if wrapper:
                                if inner_type_str is None:
                                    raise ValueError(
                                        f"Could not parse inner type for '{output_type}'"
                                    )
                                # Handle List[type] or Dict[str,type]
                                inner_mapping = None
                                if "." in inner_type_str:
                                    inner_mapping = inner_type_str
                                else:
                                    try:
                                        inner_model = await import_class_by_name(
                                            inner_type_str, db
                                        )
                                    except (ValueError, LookupError):
                                        inner_model = None
                                    if inner_model is not None:
                                        inner_mapping = self.get_class_mapping(
                                            inner_model, drops
                                        )
                                if inner_mapping is not None:
                                    if wrapper == "List":
                                        output_mapping = f"List[{inner_mapping}]"
                                    else:  # Dict
                                        output_mapping = f"Dict[str,{inner_mapping}]"
                            elif "." in output_type:
                                # It's a full mapping path
                                output_mapping = output_type
                            else:
                                # It's a single class name
                                try:
                                    output_model = await import_class_by_name(
                                        output_type, db
                                    )
                                    output_mapping = self.get_class_mapping(
                                        output_model, drops
                                    )
                                except (ValueError, LookupError) as e:
                                    logger.error(
                                        f"Could not parse '{data['type']}' to mapping: {e}"
                                    )
                                    output_mapping = None
                            if output_mapping is not None:
                                result_mappings.append(
                                    DataMapping(
                                        name=name,
                                        mapping=output_mapping,
                                        description=data.get("description"),
                                    )
                                )
                        except (ValueError, LookupError) as e:
                            logger.error(
                                f"Could not parse '{data['type']}' to mapping: {e}"
                            )
        else:  # not a SimstackResult
            for output in outputs:
                try:
                    output_type = output["type"]
                    if isinstance(output_type, str):
                        output_mapping = output_type
                        if " | None" in output_mapping:
                            output_mapping = output_mapping.replace(" | None", "")
                    else:
                        # Handle Optional[T] / T | None
                        from typing import get_args, get_origin, Union
                        import types
                        origin = get_origin(output_type)
                        if origin is types.UnionType or origin is Union:
                            args = get_args(output_type)
                            if type(None) in args:
                                # It's an Optional, take the first non-None argument
                                output_type = next(arg for arg in args if arg is not type(None))
                        output_mapping = self.get_class_mapping(output_type, drops)
                    result_mappings.append(
                        DataMapping(
                            name=output["name"],
                            mapping=output_mapping,
                            description=output.get("description"),
                        )
                    )
                except ValueError:
                    logger.error(f"Could not parse '{output['type']}' to mapping in {func_name}")
        return result_mappings

    def _extract_default_parameters(self, func: Callable[..., Any]) -> Parameters:
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
            existing_model = await self.db.find_one(
                NodeModel, NodeModel.name == node_name
            )
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
                await self.db.delete(existing_model.pickle_function)
            except Exception as e:
                logger.error(f"Error deleting FunctionPickle for {node_name}: {e}")

        try:
            await self.db.delete(existing_model)
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

                    input_mapping_found = await self.db.find_one(
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

    def get_class_mapping(self, type: Type, drops: str = "") -> str:
        """Return the class mapping for a given type, optionally dropping a prefix."""
        if hasattr(type, "__module__") and hasattr(type, "__name__"):
            mapping = type.__module__ + "." + type.__name__
            if drops and mapping.startswith(drops + "."):
                mapping = mapping[len(drops) + 1 :]
            return mapping
        else:
            raise ValueError(f"Could not parse '{type}' to mapping")

    async def _register_nodes_from_module(self, module: Any, drops: str) -> None:
        """
        Core logic to discover node functions in a module and (re)create NodeModel entries.

        Heuristic:
        - All top-level callables (functions) whose names do not start with '_'
        - Only functions actually defined in this module.
        """
        functions = self._discover_module_functions(module)
        code_source = getattr(module, "_simstack_code_source", None)

        for func_name, func in functions:
            if not is_node_function(func):
                continue

            sig = inspect.signature(func)

            parser = DocstringParser(inspect.getdoc(func))
            doc_description = parser.description()
            doc_params = parser.params()

            type_hints = get_type_hints(func)

            inputs = self._build_inputs(sig, type_hints, doc_params)

            parameters = self._extract_default_parameters(func)
            node_name = getattr(func, "_node_name", func_name)
            node_description = getattr(func, "_node_description", doc_description or "")

            if not inputs:
                logger.warning(
                    f"{node_name} has no inputs -- this means the node will be executed only once."
                )

            input_mappings = await self._resolve_input_mappings(
                node_name, inputs, drops
            )
            function_mapping = module.__name__ + "." + func_name
            try:
                (
                    should_skip,
                    existing_favorite,
                ) = await self._delete_existing_node_model_if_needed(
                    node_name, function_mapping
                )
                if should_skip:
                    continue

                data_mappings = []
                for data_input, input_mapping in zip(inputs, input_mappings):
                    if data_input.get("type") and hasattr(
                        data_input["type"], "__name__"
                    ):
                        data_input_mapping = self.get_class_mapping(
                            data_input["type"], drops
                        )
                        if data_input_mapping != input_mapping:
                            self.logger.error(
                                f"Type mismatch for input '{data_input['name']}': expected '{data_input['type'].__name__}', got '{input_mapping}'"
                            )
                    else:
                        self.logger.error(
                            f"No type specified for input '{data_input['name']}'"
                        )
                    data_mappings.append(
                        DataMapping(
                            name=data_input["name"],
                            mapping=input_mapping,
                            description=data_input.get("description"),
                        )
                    )

                result_mappings = await self._build_outputs(
                    func_name, type_hints, parser, drops
                )

                if node_name is None:
                    raise ValueError(f"Node {func_name} has no name")

                node_model = NodeModel(
                    name=node_name,
                    function_mapping=function_mapping,
                    version=getattr(func, "_node_version", None),
                    description=node_description,
                    input_mappings=data_mappings,
                    result_mappings=result_mappings,
                    called_nodes=[],  # we need to first build the full list, will be filled in second_stage
                    expose_in_submit=getattr(func, "_node_expose_in_submit", True),
                    code_source=code_source,
                    default_parameters=parameters,
                    pickle_function=None,
                    favorite=existing_favorite,
                )

                logger.debug(
                    f"NodeModel: {node_model.name}, {node_model.function_mapping}, {node_model.input_mappings}"
                )
                await self.db.save(node_model)

            except Exception as e:
                logger.error(f"Error creating/saving NodeModel {node_name}: {e}")
                import traceback

                traceback.print_exc()

    async def second_stage(self, drops: str) -> None:
        await update_node_children(self.db, drops)

    async def clear_table(self) -> None:
        self.logger.info("Clearing NodeModel collection")
        await self.db.get_collection(NodeModel).drop()


async def make_node_table(
    db: Database,
    dirs: list[str] | None = None,
    drops: str | None = None,
    write_schema: bool = False,
    clear: bool = False,
    project_root: Path | None = None,
    ignore_entrypoints: bool = False,
    refresh_mappings: bool = True,
) -> None:
    """
    Rebuild the node table using the given databse.

    This is a thin wrapper around CreateNodeTable for backward compatibility.
    """
    creator = CreateNodeTable(db, write_schema=write_schema, project_root=project_root)
    await creator.build(
        dirs=dirs,
        drops=drops,
        clear=clear,
        ignore_entrypoints=ignore_entrypoints,
        refresh_mappings=refresh_mappings,
    )


def create_node_table_main() -> None:
    """
    CLI-style entry point to (re)build the node table.

    Uses a dedicated event loop, matching the pattern used for model table creation.
    """
    TableBuilderBase.cli_main(CreateNodeTable)


if __name__ == "__main__":
    create_node_table_main()
