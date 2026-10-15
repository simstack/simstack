"""Resolve registered model and node mappings without compatibility fallbacks.

Callers either provide a canonical Python path or deliberately use the separate
name-based entry point. Path lookups never degrade to name lookups, and all
registry access goes through the explicitly injected repository.
"""

import importlib
import logging
from typing import Any, Callable, Type, cast

from simstack.models.document import Model, ObjectId
from simstack.models.models import ModelMapping, NodeModel
from simstack.persistence import Repository, eq

logger = logging.getLogger("importer")


async def _find_node_model(
    function_path: str, repository: Repository
) -> NodeModel | None:
    """Return the node registered for exactly ``function_path``."""
    return await repository.find_one(NodeModel, eq("function_mapping", function_path))


async def _find_node_model_by_name(
    function_name: str, repository: Repository
) -> NodeModel | None:
    """Return the node registered for exactly ``function_name``."""
    return await repository.find_one(NodeModel, eq("name", function_name))


async def _find_model_mapping(
    model_path: str, repository: Repository
) -> ModelMapping | None:
    """Return the model registered for exactly ``model_path``."""
    return await repository.find_one(ModelMapping, eq("mapping", model_path))


async def _find_model_mapping_by_name(
    class_name: str, repository: Repository
) -> ModelMapping | None:
    """Return the model registered for exactly ``class_name``."""
    return await repository.find_one(ModelMapping, eq("name", class_name))


async def _function_from_model(
    node_model: NodeModel, task_id: ObjectId | None = None
) -> Callable[..., Any]:
    """Import the exact canonical function path stored in ``node_model``."""
    function_path = node_model.function_mapping
    try:
        module_path, function_name = function_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return cast(Callable[..., Any], getattr(module, function_name))
    except (ImportError, AttributeError, ValueError) as exc:
        logger.error(
            "task_id: %s Error importing function %s: %s",
            task_id,
            function_path,
            exc,
        )
        raise


async def import_function(
    function_path: str,
    repository: Repository,
    task_id: ObjectId | None = None,
) -> Callable[..., Any]:
    """Import a function from its exact registered or canonical Python path.

    A missing registry record does not trigger a name lookup. Direct Python
    import is allowed only for the exact canonical path supplied by the caller.
    """
    node_model = await _find_node_model(function_path, repository)

    if node_model is not None:
        return await _function_from_model(node_model, task_id)

    try:
        module_path, function_name = function_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return cast(Callable[..., Any], getattr(module, function_name))
    except (ImportError, AttributeError, ValueError) as exc:
        raise LookupError(
            f"task_id: {task_id} Function {function_path} not found in the NodeModel Table"
        ) from exc


async def import_function_by_name(
    function_name: str, repository: Repository, task_id: ObjectId | None
) -> Callable[..., Any]:
    """Import a function through an explicit exact-name registry lookup."""
    node_model = await _find_node_model_by_name(function_name, repository)
    if node_model is None:
        raise LookupError(f"No function mapping registered for {function_name!r}")
    return await _function_from_model(node_model, task_id)


async def import_class(class_path: str, repository: Repository) -> Type[Model]:
    """Import a model from its exact registered or canonical Python path.

    A missing registry record does not trigger a class-name lookup. Direct
    Python import is allowed only for the exact canonical path supplied by the
    caller.
    """
    try:
        module_path, class_name = class_path.rsplit(".", 1)
    except ValueError as exc:
        raise LookupError(f"Model path must be canonical: {class_path!r}") from exc

    model_mapping = await _find_model_mapping(class_path, repository)
    if model_mapping is not None:
        module_path, class_name = model_mapping.mapping.rsplit(".", 1)

    try:
        module = importlib.import_module(module_path)
        return cast(Type[Model], getattr(module, class_name))
    except (ImportError, AttributeError) as exc:
        logger.error("Error importing class %s: %s", class_path, exc)
        raise LookupError(f"No model registered or importable at {class_path!r}") from exc


async def import_class_by_name(
    class_name: str, repository: Repository
) -> Type[Model]:
    """Import a model through an explicit exact-name registry lookup."""
    model_mapping = await _find_model_mapping_by_name(class_name, repository)
    if model_mapping is None:
        raise LookupError(f"No model mapping registered for {class_name!r}")

    return await import_class(model_mapping.mapping, repository)
