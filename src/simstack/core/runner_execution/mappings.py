"""Resolve server-advertised model and node mappings to runtime objects."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from simstack.core.context import context
from simstack.core.node_code_version import node_code_version
from simstack.models.document import Model
from simstack.models.node_registry import NodeRegistry

from .errors import RunnerTaskPayloadError


def _effective_node_version(function, binding):
    """Resolve the version from the task's exact installed source snapshot."""
    declared = getattr(function, "_node_version", None)
    if declared is not None:
        return declared
    revision = getattr(binding, "workflow_revision", None)
    if revision is None:
        return None
    from simstack.runner_runtime.workflow_installation import installation_root
    from simstack.workflows.discovery import effective_node_version

    try:
        root = installation_root(
            Path(context.config.workdir),
            getattr(binding, "workflow_installation_id", None),
            revision,
        )
        return effective_node_version(function, root / "source")
    except (OSError, TypeError, ValueError) as exc:
        raise RunnerTaskPayloadError("Cannot resolve the node version from its pinned workflow snapshot") from exc


def _import_path(path: str) -> Any:
    try:
        module_path, attribute_path = path.rsplit(".", 1)
    except ValueError as exc:
        raise RunnerTaskPayloadError(f"Invalid import mapping: {path!r}") from exc
    try:
        value = importlib.import_module(module_path)
        for attribute in attribute_path.split("."):
            value = getattr(value, attribute)
        return value
    except (ImportError, AttributeError) as exc:
        raise RunnerTaskPayloadError(f"Could not import mapping {path!r}") from exc


def _model_mapping(variable_mapping: str):
    mappings = context.model_mappings
    if mappings is None:
        raise RunnerTaskPayloadError("Runner bootstrap has no model mappings")
    mapping = mappings.get_by_mapping(variable_mapping)
    if mapping is None:
        raise RunnerTaskPayloadError(
            f"Model mapping {variable_mapping!r} is absent from runner bootstrap"
        )
    return mapping


def _mapping_for_model(model: Model | type[Model]):
    model_class = model if isinstance(model, type) else type(model)
    mappings = context.model_mappings
    if mappings is None:
        raise RunnerTaskPayloadError("Runner bootstrap has no model mappings")
    canonical_path = f"{model_class.__module__}.{model_class.__qualname__}"
    mapping = mappings.get_by_mapping(canonical_path)
    if mapping is None:
        raise RunnerTaskPayloadError(
            f"Runtime model {canonical_path} does not have a canonical "
            "bootstrap mapping"
        )
    return mapping


def _model_class(variable_mapping: str) -> type[Model]:
    mapping = _model_mapping(variable_mapping)
    value = _import_path(str(mapping.mapping))
    if not isinstance(value, type) or not issubclass(value, Model):
        raise RunnerTaskPayloadError(
            f"Mapped input {mapping.mapping!r} is not a SimStack document model"
        )
    return cast(type[Model], value)


def _node_model_for_registry(registry: NodeRegistry) -> Any:
    mappings = context.node_mappings
    if mappings is None:
        raise RunnerTaskPayloadError("Runner bootstrap has no node mappings")
    node_model = mappings.get_by_mapping(registry.func_mapping)
    if node_model is None:
        raise RunnerTaskPayloadError(
            f"Node {registry.func_mapping!r} is absent from runner bootstrap"
        )
    return node_model


def _function_for_registry(
    registry: NodeRegistry,
    *,
    node_model: Any | None = None,
) -> Callable[..., Any]:
    if node_model is None:
        node_model = _node_model_for_registry(registry)
    mapped_path = str(node_model.function_mapping)
    if mapped_path != registry.func_mapping:
        raise RunnerTaskPayloadError(
            f"Task function {registry.func_mapping!r} does not match bootstrap mapping"
        )
    wrapped = _import_path(mapped_path)
    function = getattr(wrapped, "_inner", wrapped)
    if not callable(function):
        raise RunnerTaskPayloadError(f"Node mapping {mapped_path!r} is not callable")
    if getattr(node_model, "workflow_revision", None) != registry.workflow_revision:
        raise RunnerTaskPayloadError("Task workflow revision does not match its pinned bootstrap catalogue")
    actual_hash = node_code_version(function, workflow_revision=registry.workflow_revision)
    if registry.function_hash not in ("", "NOT INITIALIZED", actual_hash):
        raise RunnerTaskPayloadError(
            f"Node code version mismatch for {registry.name}: "
            f"submitted {registry.function_hash}, worker {actual_hash}"
        )
    declared_version = _effective_node_version(function, registry)
    if registry.version is not None and registry.version != declared_version:
        raise RunnerTaskPayloadError(
            f"Declared node version mismatch for {registry.name}: "
            f"submitted {registry.version!r}, worker {declared_version!r}"
        )
    return cast(Callable[..., Any], function)
