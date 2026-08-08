import ast
import logging
from typing import Any, Callable, cast

from simstack.models.models import NodeModel

logger = logging.getLogger("dynamic_node_loader")


def _resolve_source(node_model: NodeModel) -> str:
    if node_model.module_source and node_model.module_source.strip():
        return node_model.module_source
    if node_model.function_code and node_model.function_code.strip():
        return node_model.function_code
    raise ValueError(
        f"NodeModel {node_model.name!r} has no stored source (function_code/module_source empty)"
    )


def _function_name_from_mapping(function_mapping: str) -> str:
    if "." not in function_mapping:
        raise ValueError(f"Invalid function_mapping: {function_mapping!r}")
    return function_mapping.rsplit(".", 1)[-1]


def _build_exec_namespace() -> dict[str, Any]:
    """Pre-inject common simstack symbols for minimal function_code snippets."""
    from simstack.core.node import node

    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "node": node,
    }
    try:
        import simstack.models as simstack_models

        for name in dir(simstack_models):
            if name.startswith("_"):
                continue
            namespace[name] = getattr(simstack_models, name)
    except ImportError:
        logger.debug("Could not pre-import simstack.models for dynamic node loader")
    return namespace


def load_node_from_source(node_model: NodeModel) -> Callable[..., Any]:
    """
    Load a node callable from stored source on the NodeModel.

    Uses module_source when available, otherwise function_code.
    """
    source = _resolve_source(node_model)
    ast.parse(source)

    module_name = f"simstack_dynamic_nodes.{node_model.function_mapping.replace('.', '_')}"
    namespace = _build_exec_namespace()
    namespace["__name__"] = module_name
    namespace["__file__"] = f"<{node_model.function_mapping}>"

    exec(compile(source, module_name, "exec"), namespace)  # noqa: S102

    function_name = _function_name_from_mapping(node_model.function_mapping)
    if function_name not in namespace:
        raise AttributeError(
            f"Stored source for {node_model.function_mapping!r} does not define {function_name!r}"
        )
    return cast(Callable[..., Any], namespace[function_name])
