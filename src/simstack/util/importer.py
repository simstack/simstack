import importlib
import inspect
import logging
from typing import Any, Callable, Optional, Type, cast
from odmantic import Model, AIOEngine, ObjectId

from simstack.core.context import context
from simstack.models.models import ModelMapping, NodeModel
from simstack.core.workflow_repository import (
    cached_repository_checkout,
    import_workflow_symbol,
)
from simstack.models.workflow_repository import CodeSource
from simstack.util.db import Database

logger = logging.getLogger("importer")

NODES_SEARCH_BY_NAME_FALLBACK = True
MODELS_SEARCH_BY_NAME_FALLBACK = True

def _get_initialized_context() -> Any | None:
    try:
        from simstack.core.context import context

        if context.initialized:
            return context
    except RuntimeError:
        return None
    return None


def _context_cache_matches_engine(ctx: Any | None, db: Database | None = None) -> bool:
    if ctx is None:
        return False
    if db is None:
        return True
    try:
        return ctx.db is not None and db is ctx.db
    except RuntimeError:
        return False


def _resolve_engine(ctx: Any | None, engine: AIOEngine | None = None) -> AIOEngine:
    if engine is not None:
        return engine
    if ctx is not None:
        try:
            if ctx.db is not None:
                return cast(AIOEngine, ctx.db.core_engine)
        except RuntimeError:
            pass
    raise RuntimeError(
        "Could not resolve engine both engine and context have no engine"
    )


def _lookup_node_cache(
    node_mappings: Any,
    function_path: str,
) -> Optional[NodeModel]:
    if node_mappings is None:
        return None
    node_model = node_mappings.get_by_mapping(function_path)
    if node_model is None and NODES_SEARCH_BY_NAME_FALLBACK:
        if "." in function_path:
            _, function_name = function_path.rsplit(".", 1)
        else:
            function_name = function_path
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
        if "." in function_path:
            _, function_name = function_path.rsplit(".", 1)
        else:
            function_name = function_path
        node_model = await db.find_one(NodeModel, NodeModel.name == function_name)
    return node_model


async def _find_node_model_by_name(
    function_name: str, db: Database
) -> Optional[NodeModel]:
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


def _lookup_model_cache(
    model_mappings: Any, class_path: str, class_name: str
) -> Optional[ModelMapping]:
    if model_mappings is None:
        return None
    model_mapping = None
    if MODELS_SEARCH_BY_NAME_FALLBACK:
        model_mapping = model_mappings.get_by_name(class_name)
    if not model_mapping:
        model_mapping = model_mappings.get_by_mapping(class_path)
    return model_mapping


async def _find_model_mapping(model_path: str, db: Database) -> Optional[ModelMapping]:
    _, model_name = model_path.rsplit(".", 1)

    ctx = _get_initialized_context()
    if _context_cache_matches_engine(ctx, db):
        if ctx.model_mappings is None:
            await ctx.refresh_mappings(models=True, nodes=False)

        model_mapping = _lookup_model_cache(
            ctx.model_mappings,
            model_path,
            model_name,
        )
        if model_mapping is not None:
            return model_mapping

        await ctx.refresh_mappings(models=True, nodes=False)
        model_mapping = _lookup_model_cache(
            ctx.model_mappings,
            model_path,
            model_name,
        )
        if model_mapping is not None:
            return model_mapping

    model_mapping = None
    if MODELS_SEARCH_BY_NAME_FALLBACK:
        model_mapping = await db.find_one(ModelMapping, ModelMapping.name == model_name)
    if model_mapping is None:
        model_mapping = await db.find_one(
            ModelMapping, ModelMapping.mapping == model_path
        )
    return model_mapping


# TODO engines remove: duplicate of find_class_mapping_by_name
async def _find_model_mapping_by_name(
    class_name: str, db: Database
) -> Optional[ModelMapping]:
    ctx = _get_initialized_context()
    if _context_cache_matches_engine(ctx, db):
        if ctx.model_mappings is None:
            await ctx.refresh_mappings(models=True, nodes=False)

        model_mapping = ctx.model_mappings.get_by_name(class_name)
        if model_mapping is not None:
            return model_mapping

        await ctx.refresh_mappings(models=True, nodes=False)
        model_mapping = ctx.model_mappings.get_by_name(class_name)
        if model_mapping is not None:
            return model_mapping

    return await db.find_one(ModelMapping, ModelMapping.name == class_name)


async def _function_from_model(
    node_model: NodeModel,
    db: Database,
    task_id: ObjectId | None = None,
) -> Callable[..., Any]:
    """
    Get the function from the NodeModel. Here the mapping may already be fixed if the original mapping was wrong
    Otherwise, it is imported from the function_mapping.

    Args:
        node_model: NodeModel object
        task_id: Optional task id

    Returns:
        The function object
    """

    function_path = node_model.function_mapping
    if node_model.code_source is not None:
        repository_function = await import_workflow_symbol(
            db, function_path, node_model.code_source
        )
        if not callable(repository_function):
            raise LookupError(f"Workflow function {function_path} was not found")
        return cast(Callable[..., Any], repository_function)
    try:
        module_path, function_name = function_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return cast(Callable[..., Any], getattr(module, function_name))
    except (ImportError, AttributeError, ValueError) as e:
        if NODES_SEARCH_BY_NAME_FALLBACK:
            try:
                # Try to load by the name field which might contain the correct path
                # if it was a name-only search that found this model.
                if "." in node_model.name:
                    module_path, function_name = node_model.name.rsplit(".", 1)
                    module = importlib.import_module(module_path)
                    return cast(Callable[..., Any], getattr(module, function_name))
            except (ImportError, AttributeError, ValueError):
                pass
        logger.error(
            f"task_id: {task_id} Error importing function {function_path}: {e}"
        )
        raise e


async def import_function(
    function_path: str,
    db: Database,
    task_id: ObjectId | None = None,
    tolerate_missing_function: bool = False,
    code_source: CodeSource | None = None,
) -> Optional[Callable[..., Any]]:
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
    if code_source is not None:
        try:
            repository_function = await import_workflow_symbol(
                db, function_path, code_source
            )
            if not callable(repository_function):
                raise LookupError(f"Workflow function {function_path} was not found")
            return cast(Callable[..., Any], repository_function)
        except Exception:
            if tolerate_missing_function:
                return None
            raise

    node_model = await _find_node_model(function_path, db)

    if node_model is None:
        try:
            module_path, function_name = function_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            return cast(Callable[..., Any], getattr(module, function_name))
        except (ImportError, AttributeError, ValueError):
            raise LookupError(
                f"task_id: {task_id} Function {function_path} not found in the NodeModel Table"
            )

    try:
        return await _function_from_model(node_model, db, task_id)
    except Exception as e:
        if tolerate_missing_function:
            return None
        else:
            raise e


async def import_function_by_name(
    function_name: str, db: Database, task_id: ObjectId
) -> Optional[Callable[..., Any]]:
    node_model = await _find_node_model_by_name(function_name, db)

    if node_model is None:
        logger.error(f"Could not find function mapping for name: {function_name}")
        raise ValueError(f"Could not find function mapping for name: {function_name}")

    return await _function_from_model(node_model, db, task_id)


async def import_class(
    class_path: str,
    db: Database,
    code_source: CodeSource | None = None,
) -> Type[Model] | None:
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

        # A task source pins only models registered by that same repository.
        # Import the originally referenced path so tasks remain reproducible
        # when a later head moves the model while retaining its unique name.
        if (
            code_source is not None
            and model_mapping is not None
            and model_mapping.code_source is not None
            and model_mapping.code_source.repo_id == code_source.repo_id
        ):
            repository_model = await import_workflow_symbol(
                db, class_path, code_source
            )
            if not inspect.isclass(repository_model):
                raise LookupError(f"Workflow model {class_path} was not found")
            return cast(Type[Model], repository_model)

        # A later activation may remove this repository's stale registration,
        # allowing another repository to claim the same mapping while an old
        # task still pins the original commit. Only try the pinned import when
        # its module is actually present so ordinary cross-repository imports
        # do not disturb installed modules through repository import setup.
        if (
            code_source is not None
            and model_mapping is not None
            and model_mapping.code_source is not None
            and model_mapping.code_source.repo_id != code_source.repo_id
        ):
            checkout = await cached_repository_checkout(db, code_source)
            pinned_module_path = checkout.joinpath(*module_path.split("."))
            if (
                pinned_module_path.with_suffix(".py").is_file()
                or (pinned_module_path / "__init__.py").is_file()
            ):
                repository_model = await import_workflow_symbol(
                    db, class_path, code_source
                )
                if not inspect.isclass(repository_model):
                    raise LookupError(f"Workflow model {class_path} was not found")
                return cast(Type[Model], repository_model)

        # If not found by name, try by mapping
        if not model_mapping:
            model_mapping = await db.find_one(
                ModelMapping, ModelMapping.mapping == class_path
            )
        else:  # when searching by name, the path may have changed
            module_path, class_name = model_mapping.mapping.rsplit(".", 1)

        if model_mapping is None and code_source is not None:
            checkout = await cached_repository_checkout(db, code_source)
            pinned_module_path = checkout.joinpath(*module_path.split("."))
            pinned_module_present = (
                pinned_module_path.with_suffix(".py").is_file()
                or (pinned_module_path / "__init__.py").is_file()
            )
            try:
                repository_model = await import_workflow_symbol(
                    db, class_path, code_source
                )
            except (ImportError, AttributeError, LookupError):
                if pinned_module_present:
                    raise
                # The reference may be an installed/built-in class whose mapping
                # was never repository-owned; retain its normal import path.
                pass
            else:
                if not inspect.isclass(repository_model):
                    raise LookupError(f"Workflow model {class_path} was not found")
                return cast(Type[Model], repository_model)

        if model_mapping is None:
            try:
                # Import the module
                module = importlib.import_module(module_path)
                # Get the class from the module
                return cast(Type[Model], getattr(module, class_name))
            except (ImportError, AttributeError):
                logger.error(f"Error finding ModelMapping for {class_name}")
                raise LookupError(f"Error finding ModelMapping for {class_name}")

        if model_mapping.code_source is not None:
            repository_model = await import_workflow_symbol(
                db, model_mapping.mapping, model_mapping.code_source
            )
            if not inspect.isclass(repository_model):
                raise LookupError(f"Workflow model {model_mapping.mapping} was not found")
            return cast(Type[Model], repository_model)

        # Import the module
        module = importlib.import_module(module_path)

        # Get the class from the module
        return cast(Type[Model], getattr(module, class_name))
    except (ImportError, AttributeError, ValueError) as e:
        logger.error(f"Error importing class {class_path}: {e}")
        raise e


async def import_class_by_name(class_name: str, db: Database) -> Type[Model]:
    model_mapping = await _find_model_mapping_by_name(class_name, db)

    if not model_mapping:
        logger.error(f"Error finding ModelMapping for {class_name}")
        raise LookupError(f"Error finding ModelMapping for {class_name}")

    model_class = await import_class(model_mapping.mapping, db)
    if model_class is None:
        raise LookupError(f"Error importing mapped model class for {class_name}")
    return model_class
