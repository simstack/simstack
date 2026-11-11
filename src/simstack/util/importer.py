"""Import pinned workflow code using explicit revision catalogue descriptors."""
import importlib
from importlib.machinery import PathFinder
import inspect
import logging
from pathlib import Path
import site
import sys
from types import SimpleNamespace
from typing import Any, Callable, Optional, Type

from simstack.models.document import Model, ObjectId

logger = logging.getLogger("importer")


def _active_api(api=None):
    if api is not None:
        return api
    from simstack.core.context import context
    return context.server.values


def _refresh_context_cache(values, descriptors, *, nodes):
    from simstack.core.context import context
    from simstack.util.mappings import ModelMappingTable, NodeMappingTable

    if context.initialized and context.server.values is values:
        table = NodeMappingTable(descriptors) if nodes else ModelMappingTable(descriptors)
        object.__setattr__(context, "_node_mappings" if nodes else "_model_mappings", table)


async def _find_node_model(function_path, api=None):
    values = _active_api(api)
    nodes = await values.catalogue_nodes()
    _refresh_context_cache(values, nodes, nodes=True)
    exact = [node for node in nodes if node.function_mapping == function_path]
    if exact:
        return exact[0]
    name = function_path.rsplit(".", 1)[-1]
    named = [node for node in nodes if node.name == name]
    if len(named) > 1:
        raise LookupError(f"Ambiguous node name: {name}")
    return named[0] if named else None


async def _find_model_mapping(model_path, api=None):
    values = _active_api(api)
    mappings = await values.catalogue_models()
    _refresh_context_cache(values, mappings, nodes=False)
    exact = [item for item in mappings if item.mapping == model_path]
    if exact:
        return exact[0]
    name = model_path.rsplit(".", 1)[-1]
    named = [item for item in mappings if item.name == name]
    if len(named) > 1:
        raise LookupError(f"Ambiguous model name: {name}")
    return named[0] if named else None


def _import_mapping(mapping):
    module_path, name = mapping.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), name)


def _artifact_module(module_path, source_root):
    """Resolve the exact helper in the workflow runtime before importing it."""
    core_root = Path(__file__).resolve().parents[1]
    roots = (source_root.resolve(strict=True), core_root,
             *(Path(path).resolve() for path in site.getsitepackages()))

    def approved(path):
        return any(Path(path).resolve().is_relative_to(root) for root in roots)

    search = [str(path) for path in (source_root, source_root / "src", core_root.parent)]
    search += [path for path in sys.path if path and approved(path)]
    parts = module_path.split(".")
    for index in range(len(parts)):
        name = ".".join(parts[:index + 1])
        loaded = sys.modules.get(name)
        spec = getattr(loaded, "__spec__", None) if loaded is not None else PathFinder.find_spec(name, search)
        origin = getattr(loaded, "__file__", None) if loaded is not None else getattr(spec, "origin", None)
        locations = getattr(loaded, "__path__", None) if loaded is not None else getattr(spec, "submodule_search_locations", None)
        if origin is not None and not approved(origin):
            raise ImportError(f"Artifact helper module {name} is outside the prepared workflow runtime")
        if origin is None and (not locations or any(not approved(path) for path in locations)):
            raise ImportError(f"Artifact helper module {name} is not in the prepared workflow runtime")
        if loaded is None:
            loaded = importlib.util.module_from_spec(spec)
            sys.modules[name] = loaded
            try:
                spec.loader.exec_module(loaded)
            except BaseException:
                del sys.modules[name]
                raise
            if index:
                setattr(sys.modules[name.rsplit(".", 1)[0]], parts[index], loaded)
        if index + 1 < len(parts):
            if not locations:
                raise ImportError(f"Artifact helper module {name} is not a package")
            search = list(locations)
    return loaded


async def import_artifact_generator(descriptor, *, task=None):
    """Import only the exact generator authorized by an artifact descriptor."""
    from simstack.core.context import context
    from simstack.models.artifact_models import ArtifactMapping
    from simstack.util.repository_runtime import prepare_task_runtime

    if not isinstance(descriptor, ArtifactMapping):
        raise TypeError("An artifact generator requires an ArtifactMapping descriptor")
    mapping = descriptor.function_mapping
    parts = mapping.split(".")
    if len(parts) < 2 or any(not part.isidentifier() for part in parts):
        raise ValueError("An artifact generator requires its full module and function path")
    revision = task.repository_revision if task is not None else context.server.requests.revision()
    root = await prepare_task_runtime(SimpleNamespace(repository_revision=revision)) if revision else context.config.project_root
    module = _artifact_module(".".join(parts[:-1]), Path(root))
    function = getattr(module, parts[-1])
    if not callable(function):
        raise TypeError(f"Artifact generator {mapping} is not callable")
    inspect.signature(function).bind(object())
    return function


async def import_function(function_path: str, api=None, task_id: ObjectId | None = None,
                          tolerate_missing_function: bool = False) -> Optional[Callable[..., Any]]:
    descriptor = await _find_node_model(function_path, api)
    if descriptor is None:
        if tolerate_missing_function:
            return None
        raise LookupError(f"task_id: {task_id} Function {function_path} is not in the revision catalogue")
    try:
        return _import_mapping(descriptor.function_mapping)
    except (ImportError, AttributeError):
        if tolerate_missing_function:
            return None
        raise


async def import_function_by_name(function_name: str, api=None, task_id=None):
    return await import_function(function_name, api, task_id)


async def import_class(class_path: str, api=None) -> Type[Model]:
    descriptor = await _find_model_mapping(class_path, api)
    if descriptor is None:
        raise LookupError(f"Model {class_path} is not in the revision catalogue")
    mapping = descriptor.mapping
    if "." not in mapping and "." in class_path:
        mapping = class_path.rsplit(".", 1)[0] + "." + mapping
    if "." not in mapping:
        raise LookupError(f"Model {class_path} has no catalogue module path")
    return _import_mapping(mapping)


async def import_class_by_name(class_name: str, api=None) -> Type[Model]:
    return await import_class(class_name, api)
