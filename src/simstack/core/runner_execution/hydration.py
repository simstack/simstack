"""Hydrate exact task wire documents into runtime models."""

from __future__ import annotations

from collections.abc import Mapping
from graphlib import CycleError, TopologicalSorter
from typing import Any, cast

from simstack.models.document import Model, decode_document_value
from simstack.models.file_instance import FileInstance
from simstack.models.node_registry import NodeRegistry
from simstack.runner_protocol import (
    RunnerProtocolError,
    validate_runner_document_entries_v1,
    validate_runner_task_v1,
)

from .errors import RunnerTaskPayloadError
from .mappings import _model_class
from .references import transform_model_references, walk_runtime_values


def node_registry_from_task(task: Mapping[str, Any]) -> NodeRegistry:
    """Validate the node document embedded in a runner task bundle."""

    try:
        payload = validate_runner_task_v1(dict(task), operation="execution")
    except RunnerProtocolError as exc:
        raise RunnerTaskPayloadError(str(exc)) from exc
    payload.pop("task_schema_version")
    payload.pop("inputs", None)
    payload.pop("results", None)
    payload.pop("artifact_plan", None)
    payload.pop("info_files", None)
    if "id" not in payload:
        raise RunnerTaskPayloadError("Runner task DTO is missing id")
    try:
        return NodeRegistry.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise RunnerTaskPayloadError("Runner task DTO is malformed") from exc


def _model_id(value: Mapping[str, Any]) -> str:
    raw_id = value.get("id")
    if not isinstance(raw_id, str) or not raw_id or raw_id != raw_id.strip():
        raise RunnerTaskPayloadError("Runner task document requires canonical id")
    return raw_id


def _hydrate_entries(entries: Any, *, kind: str) -> list[tuple[str, Model]]:
    try:
        validate_runner_document_entries_v1(
            entries,
            operation=f"task {kind}",
        )
    except RunnerProtocolError as exc:
        raise RunnerTaskPayloadError(str(exc)) from exc

    flattened: list[tuple[Mapping[str, Any], bool]] = []
    for entry in entries:
        flattened.extend((dependency, False) for dependency in entry["dependencies"])
        flattened.append((entry, True))

    raw_by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for entry, _ in flattened:
        variable_mapping = entry.get("variable_mapping")
        value = entry.get("value")
        if not isinstance(variable_mapping, str) or not variable_mapping:
            raise RunnerTaskPayloadError(
                f"Runner task {kind} value has no variable_mapping"
            )
        if not isinstance(value, Mapping):
            raise RunnerTaskPayloadError(
                f"Runner task {kind} value for {variable_mapping!r} is not an object"
            )
        decoded = decode_document_value(value)
        if not isinstance(decoded, Mapping):  # defensive: a document stays an object
            raise RunnerTaskPayloadError(
                f"Runner task {kind} value for {variable_mapping!r} is not a document"
            )
        value_id = _model_id(decoded)
        existing = raw_by_id.get(value_id)
        candidate = (variable_mapping, decoded)
        if existing is not None and existing != candidate:
            raise RunnerTaskPayloadError(
                f"Runner task {kind} contains conflicting document id {value_id}"
            )
        raw_by_id[value_id] = candidate

    model_classes = {identifier: _model_class(mapping) for identifier, (mapping, _) in raw_by_id.items()}
    graph: dict[str, set[str]] = {identifier: set() for identifier in raw_by_id}

    def reference_id(value: Any, target_model: type[Model], path: str) -> str:
        if isinstance(value, (Mapping, Model)):
            raise RunnerTaskPayloadError(f"Referenced document {path} must be flattened into dependencies")
        identifier = str(value)
        dependency = raw_by_id.get(identifier)
        if dependency is None:
            raise RunnerTaskPayloadError(
                f"Runner task {kind} is missing referenced model {identifier} required by {path}"
            )
        if model_classes[identifier] is not target_model:
            raise RunnerTaskPayloadError(f"Referenced document type mismatch for {path}")
        return identifier

    for identifier, (_, raw_value) in raw_by_id.items():
        def collect(value: Any, target_model: type[Model], path: str, identifier=identifier) -> Any:
            graph[identifier].add(reference_id(value, target_model, path))
            return value
        transform_model_references(raw_value, model_classes[identifier], collect, path=model_classes[identifier].__name__)
    try:
        ordered = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise RunnerTaskPayloadError(f"Cyclic model references in runner task {kind}") from exc

    hydrated_by_id: dict[str, Model] = {}
    for identifier in ordered:
        variable_mapping, raw_value = raw_by_id[identifier]
        def resolve(value: Any, target_model: type[Model], path: str) -> Model:
            return hydrated_by_id[reference_id(value, target_model, path)]
        prepared = transform_model_references(raw_value, model_classes[identifier], resolve, path=model_classes[identifier].__name__)
        try:
            hydrated_by_id[identifier] = model_classes[identifier].model_validate(prepared)
        except (TypeError, ValueError) as exc:
            raise RunnerTaskPayloadError(f"Could not hydrate {kind} model {variable_mapping!r}") from exc

    hydrated_entries: list[tuple[str, Model]] = []
    for entry, exposed in flattened:
        variable_name = entry.get("variable_name")
        variable_mapping = entry.get("variable_mapping")
        raw_value = decode_document_value(entry.get("value"))
        if exposed and (not isinstance(variable_name, str) or not variable_name):
            raise RunnerTaskPayloadError(
                f"Runner task {kind} value has no variable_name"
            )
        if not isinstance(variable_mapping, str) or not isinstance(raw_value, Mapping):
            raise RunnerTaskPayloadError(f"Runner task {kind} value is malformed")
        model = hydrated_by_id[_model_id(raw_value)]
        if exposed:
            hydrated_entries.append((cast(str, variable_name), model))

    for value in walk_runtime_values(list(hydrated_by_id.values())):
        elements = getattr(value, "elements", None)
        set_cache = getattr(value, "_set_cache", None)
        if isinstance(elements, list) and callable(set_cache) and elements:
            missing = [str(item) for item in elements if str(item) not in hydrated_by_id]
            if missing:
                raise RunnerTaskPayloadError(f"Runner task {kind} is missing list elements: {', '.join(missing)}")
            hydrated_elements = [hydrated_by_id[str(item)] for item in elements]
            get_model_class = getattr(value, "_get_model_class", None)
            if not callable(get_model_class):
                raise RunnerTaskPayloadError(f"Runner task {kind} list model has no target model contract")
            try:
                target_model = get_model_class()
            except (TypeError, RuntimeError) as exc:
                raise RunnerTaskPayloadError(f"Runner task {kind} list target model is invalid") from exc
            if not isinstance(target_model, type) or not all(isinstance(item, target_model) for item in hydrated_elements):
                raise RunnerTaskPayloadError(f"Runner task {kind} list dependency type does not match {type(value).__name__}")
            set_cache(hydrated_elements)

    for value in walk_runtime_values(list(hydrated_by_id.values())):
        if isinstance(value, FileInstance):
            instance_id = value.id
            if not isinstance(instance_id, str) or not instance_id or instance_id != instance_id.strip():
                raise RunnerTaskPayloadError(f"Runner task {kind} contains a FileInstance without canonical persisted id")
    from simstack.models.dataset_storage import bind_dataset_sections
    bind_dataset_sections([model for _, model in hydrated_entries])
    return hydrated_entries


def hydrate_task_inputs(task: Mapping[str, Any]) -> list[Model]:
    """Hydrate ordered root inputs from an API task bundle."""

    return [value for _, value in _hydrate_entries(task.get("inputs"), kind="inputs")]
