import ast
import importlib
import inspect
import os
from typing import Optional, Callable, List, Dict
from simstack.models import NodeModel
from simstack.util.importer import import_function
from simstack.util.docstring_parser import DocstringParser

import logging
logger = logging.getLogger("node_children")

def _import_callable_from_mapping(function_mapping: str) -> Optional[Callable]:
    """
    Import a callable from a 'module.submodule.func' mapping string.
    """
    try:
        module_path, _, attr = function_mapping.rpartition(".")
        if not module_path or not attr:
            logger.warning(f"Invalid function_mapping '{function_mapping}' (expected 'module.func').")
            return None

        module = importlib.import_module(module_path)
        obj = getattr(module, attr, None)
        if obj is None or not callable(obj):
            logger.warning(f"Could not resolve callable for '{function_mapping}'.")
            return None

        return obj
    except Exception as e:
        logger.warning(f"Could not import '{function_mapping}': {e}")
        return None

def _extract_called_functions(func: Callable) -> List[str]:
    """
    Inspect the function code and return all the function calls made within it.

    Returns:
        List of function names called within the function.
    """
    called_functions: List[str] = []

    try:
        source = inspect.getsource(func)
        tree = ast.parse(source)

        task_creators = {"create_tasks", "ensure_future"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    # Direct function call: func_name()
                    called_functions.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    # Attribute function call: obj.method()
                    if isinstance(node.func.value, ast.Name):
                        called_functions.append(f"{node.func.value.id}.{node.func.attr}")
                    else:
                        called_functions.append(node.func.attr)

                # Detect async task creation: create_tasks(func()), ensure_future(func()), etc.
                if isinstance(node.func, ast.Name) and node.func.id in task_creators:
                    if node.args and isinstance(node.args[0], ast.Call):
                        task_call = node.args[0]
                        if isinstance(task_call.func, ast.Name):
                            called_functions.append(task_call.func.id)
                        elif isinstance(task_call.func, ast.Attribute):
                            if isinstance(task_call.func.value, ast.Name):
                                called_functions.append(f"{task_call.func.value.id}.{task_call.func.attr}")
                            else:
                                called_functions.append(task_call.func.attr)

                elif isinstance(node.func, ast.Attribute) and node.func.attr in task_creators:
                    if node.args and isinstance(node.args[0], ast.Call):
                        task_call = node.args[0]
                        if isinstance(task_call.func, ast.Name):
                            called_functions.append(task_call.func.id)
                        elif isinstance(task_call.func, ast.Attribute):
                            if isinstance(task_call.func.value, ast.Name):
                                called_functions.append(f"{task_call.func.value.id}.{task_call.func.attr}")
                            else:
                                called_functions.append(task_call.func.attr)

    except Exception as e:
        logger.warning(f"Could not extract called functions from {getattr(func, '__name__', '<unknown>')}: {e}")

    return called_functions

_UNICODE_DASHES = str.maketrans({
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2015": "-",  # horizontal bar
    "\u2212": "-",  # minus sign
})


def normalize_text(s: str) -> str:
    # Fold punctuation that Windows charmap/cp1252 cannot encode, then
    # round-trip as UTF-8 so the dump file never raises UnicodeEncodeError.
    return s.translate(_UNICODE_DASHES).encode("utf-8", errors="replace").decode("utf-8")

async def update_node_children(database, drops: str) -> None:
    """
    1) Read all registered nodes from NodeModel.
    2) For each node, import its function and extract called functions.
    3) Keep only those calls that correspond to nodes in the table.
    4) Update the NodeModel.called_nodes of the original function with a list of *function_mapping* strings.
    """
    node_models: List[NodeModel] = await database.find(NodeModel)

    # Build lookup tables to resolve extracted names -> NodeModel.function_mapping
    mapping_by_node_name: Dict[str, str] = { nm.name: nm.function_mapping for nm in node_models}
    mapping_set: set[str] = set(nm.function_mapping for nm in node_models)

    for nm in node_models:
        try:
            func = await import_function(nm.function_mapping, database)
        except (Exception, SystemExit) as exc:
            logger.warning(
                "Preserving NodeModel id=%s name=%s mapping=%s after import failure: %s",
                nm.id,
                nm.name,
                nm.function_mapping,
                exc,
            )
            continue

        parser = DocstringParser(inspect.getdoc(func))

        called = _extract_called_functions(func)
        logger.debug(f"Node {nm.name} called: {called}")
        called_from_docstring = parser.called_nodes()
        resolved: set[str] = set()
        for called_name in called:
            # If the extractor ever returns a full mapping, accept it directly
            if called_name in mapping_set:
                resolved.add(called_name)
                continue

            # If it matches a node name, resolve via node name
            if called_name in mapping_by_node_name:
                resolved.add(mapping_by_node_name[called_name])
                continue

            # Otherwise, resolve by last segment (handles "obj.method" and "module.func")
            short = called_name.split(".")[-1]
            if short in mapping_by_node_name:
                resolved.add(mapping_by_node_name[short])
                if called_from_docstring is not None and short in called_from_docstring:
                    del called_from_docstring[short]
                continue

        # go over the leftovers in the docstring
        if called_from_docstring is not None:
            for called_name in called_from_docstring:
                if called_name in mapping_set:
                    resolved.add(called_name)
                    continue

                # If it matches a node name, resolve via node name
                if called_name in mapping_by_node_name:
                    resolved.add(mapping_by_node_name[called_name])
                    continue

                # Otherwise, resolve by last segment (handles "obj.method" and "module.func")
                # short = called_name.split(".")[-1]
                # if short in mapping_by_node_name:
                #     resolved.add(mapping_by_short_name[short])
                #     continue

        if len(resolved) != 0:
            logger.debug(f"Resolved children of {nm.name} to {resolved}")
        nm.called_nodes = sorted(resolved)

        
        await database.get_collection(NodeModel).update_one(
            {"_id": nm.id},
            {"$set": {"called_nodes": nm.called_nodes}},
        )

    with open("node_models.txt", "w", encoding="utf-8") as outfile:
        for nm in node_models:
            mapping_set.add(nm.function_mapping)
            mapping_by_node_name[nm.name] = nm.function_mapping

            # Write formatted NodeModel information
            outfile.write(f"{'=' * 80}\n")
            
            outfile.write(f"Node: {nm.name if nm.name else 'WARNING: Missing name'}\n")
            outfile.write(
                f"Function Mapping: {nm.function_mapping if nm.function_mapping else 'WARNING: Missing function_mapping'}\n")

            if hasattr(nm, 'description') and nm.description:
                try:
                    cleaned_description = normalize_text(nm.description)
                    outfile.write(f"Description: {cleaned_description}\n")
                except Exception as e:
                    logger.warning(f"Could not normalize description for {nm.name}: {e}")
            else:

                outfile.write(f"Description: WARNING: Missing description\n")

            # Write input_mappings in table format
            outfile.write(f"\nInput Mappings:\n")
            if hasattr(nm, 'input_mappings') and nm.input_mappings:
                outfile.write(f"  {'Arg Name':<20} | {'Model':<50} | {'Field':<20}\n")
                outfile.write(f"  {'-' * 20}-+-{'-' * 50}-+-{'-' * 20}\n")
                for mapping in nm.input_mappings:
                    arg_name = getattr(mapping, 'name', 'N/A')
                    model = getattr(mapping, 'mapping', 'N/A')
                    field = getattr(mapping, 'description', 'N/A')
                    outfile.write(f"  {str(arg_name):<20} | {str(model):<50} | {str(field):<20}\n")
            else:
                outfile.write(f"  (none)\n")

            # Write result_mappings in table format
            outfile.write(f"\nResult Mappings:\n")
            if hasattr(nm, 'result_mappings') and nm.result_mappings:
                outfile.write(f"  {'Arg Name':<20} | {'Model':<50} | {'Field':<20}\n")
                outfile.write(f"  {'-' * 20}-+-{'-' * 50}-+-{'-' * 20}\n")
                for mapping in nm.result_mappings:
                    arg_name = getattr(mapping, 'name', 'N/A')
                    model = getattr(mapping, 'mapping', 'N/A')
                    field = getattr(mapping, 'description', 'N/A')
                    outfile.write(f"  {str(arg_name):<20} | {str(model):<50} | {str(field):<20}\n")
            else:
                outfile.write(f"  (none)\n")

            # Write called_nodes (node children)
            outfile.write(f"\nCalled Nodes (Children):\n")
            if hasattr(nm, 'called_nodes') and nm.called_nodes:
                for called_node in nm.called_nodes:
                    outfile.write(f"  - {called_node}\n")
            else:
                outfile.write(f"  (none)\n")

            outfile.write(f"{'-' * 80}\n\n")
