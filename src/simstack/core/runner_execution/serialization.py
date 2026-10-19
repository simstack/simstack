"""Serialize runtime results and rebuild completed child results."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel

from simstack.core.simstack_result import SimstackResult
from simstack.models.document import REFERENCE_SCHEMA_KEY, Model, encode_document_value
from simstack.models.document.reference_values import transform_reference_values
from simstack.util.object_list_mixin import ObjectListMixin
from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus

from .errors import RunnerTaskExecutionError
from .hydration import _hydrate_entries
from .mappings import _mapping_for_model, _model_class
from .state import RunnerCommitPayload

_INTERNAL_RESULT_PREFIX = "__simstack_dependency_"


def _model_document(model: Model) -> dict[str, Any]:
    raw = model.model_dump_doc()
    if "_id" not in raw or "id" in raw:
        raise RunnerTaskExecutionError(
            "A model did not provide exactly one canonical document identifier"
        )
    raw["id"] = str(raw.pop("_id"))
    encoded = encode_document_value(raw)
    if not isinstance(encoded, dict):  # pragma: no cover - model dumps are objects
        raise RunnerTaskExecutionError("A model did not serialize to an object")
    return cast(dict[str, Any], encoded)


def _result_entry(model: Model, *, variable_name: str) -> dict[str, Any]:
    mapping = _mapping_for_model(model)
    entry = {
        "variable_name": variable_name,
        "variable_mapping": str(mapping.mapping),
        "value": _model_document(model),
    }
    entry["dependencies"] = _document_dependencies(model)
    return entry


def _typed_document_entry(model: Model) -> dict[str, Any]:
    mapping = _mapping_for_model(model)
    entry = {
        "variable_mapping": str(mapping.mapping),
        "value": _model_document(model),
    }
    entry["dependencies"] = _document_dependencies(model)
    return entry


def _build_declared_value_model(
    declared_mapping: Any,
    payload: Mapping[str, Any],
    *,
    purpose: str,
) -> Model:
    model_class = _model_class(str(declared_mapping.mapping))
    try:
        return model_class.model_validate(dict(payload))
    except (TypeError, ValueError) as exc:
        raise RunnerTaskExecutionError(
            f"Declared {purpose} mapping {declared_mapping.mapping!r} "
            "cannot represent the returned value"
        ) from exc


def _declared_result_models(
    visible_models: list[tuple[str, Model]],
    declared_mappings: Sequence[Any],
    *,
    infer_single_name: bool,
) -> list[tuple[str, Model]]:
    declared = Counter(
        (str(mapping.name), str(mapping.mapping)) for mapping in declared_mappings
    )
    resolved: list[tuple[str, Model]] = []
    for candidate_name, model in visible_models:
        mapping = _mapping_for_model(model)
        mapping_path = str(mapping.mapping)
        exact_pair = (candidate_name, mapping_path)
        if declared[exact_pair] > 0:
            declared[exact_pair] -= 1
            resolved.append((candidate_name, model))
            continue

        mapping_pairs = [
            pair
            for pair, count in declared.items()
            if count > 0 and pair[1] == mapping_path
        ]
        if infer_single_name and len(visible_models) == 1 and len(mapping_pairs) == 1:
            declared_pair = mapping_pairs[0]
            declared[declared_pair] -= 1
            resolved.append((declared_pair[0], model))
            continue

        raise RunnerTaskExecutionError(
            "Node returned an undeclared result pair "
            f"({candidate_name!r}, {mapping_path!r})"
        )
    missing = [pair for pair, count in declared.items() if count > 0]
    if missing:
        raise RunnerTaskExecutionError(
            "Node omitted declared result pairs: "
            + ", ".join(f"({name!r}, {mapping!r})" for name, mapping in missing)
        )
    return resolved


def _dependent_models(model: Model) -> list[Model]:
    """Collect documents behind declared reference edges, traversing embeddings.

    A nested Model is an embedded value unless its owning field is Reference.
    Embedded models still contain their own references (for example a table's
    payload FileStack), which must be included in the dependency closure.
    """
    dependencies: list[Model] = []
    seen: set[tuple[type[Model], str]] = {(type(model), str(model.id))}
    traversed: set[int] = set()
    pending: list[tuple[Any, bool]] = [(model, False)]
    while pending:
        value, referenced = pending.pop()
        if referenced and isinstance(value, Model):
            key = (type(value), str(value.id))
            if key not in seen:
                seen.add(key)
                dependencies.append(value)
        if id(value) in traversed:
            continue
        traversed.add(id(value))
        children: list[tuple[Any, bool]] = []
        if isinstance(value, BaseModel):
            values = object.__getattribute__(value, "__dict__")
            for name, field in type(value).model_fields.items():
                item = values[name]
                if (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                    def collect(child, _target, _path):
                        children.append((child, True))
                        return child
                    transform_reference_values(item, field.annotation, collect, path=name)
                else:
                    children.append((item, False))
            children.extend((item, False) for item in (value.model_extra or {}).values())
            if isinstance(value, ObjectListMixin):
                identifiers = {str(identifier) for identifier in value.elements}
                children.extend((item, True) for item in value._get_cache()
                                if isinstance(item, Model) and str(item.id) in identifiers)
        elif isinstance(value, Mapping):
            children.extend((item, False) for item in value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            children.extend((item, False) for item in value)
        pending.extend(reversed(children))
    return dependencies


def _document_dependencies(model: Model) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for dependency in _dependent_models(model):
        mapping = _mapping_for_model(dependency)
        dependencies.append(
            {
                "variable_mapping": str(mapping.mapping),
                "reference": str(dependency.id),
                "value": _model_document(dependency),
            }
        )
    return dependencies


def serialize_runner_result(
    result: Any,
    *,
    runner_info_files: Sequence[Model] = (),
    result_mappings: Sequence[Any],
) -> RunnerCommitPayload:
    """Convert an execution result to the task-domain commit contract."""

    if result is None:
        raise RunnerTaskExecutionError("Node returned None")
    if isinstance(result, bool):
        if not result:
            raise RunnerTaskExecutionError("Node returned False")
        if len(result_mappings) != 1:
            raise RunnerTaskExecutionError(
                "A boolean node result requires exactly one declared result mapping"
            )
        declared = result_mappings[0]
        visible_models: list[tuple[str, Model]] = [
            (
                str(declared.name),
                _build_declared_value_model(
                    declared,
                    {"value": True},
                    purpose="boolean result",
                ),
            )
        ]
        message = None
        result_info_files: list[Model] = []
        infer_single_name = False
    elif isinstance(result, SimstackResult):
        raw_status = (
            result.status.value
            if isinstance(result.status, TaskStatus)
            else str(result.status)
        )
        if raw_status != TaskStatus.COMPLETED.value:
            reason = result.error_message or result.message or raw_status
            raise RunnerTaskExecutionError(f"Node returned {raw_status}: {reason}")

        visible_models = []
        if result.files:
            files_mappings = [
                mapping for mapping in result_mappings if str(mapping.name) == "files"
            ]
            if len(files_mappings) != 1:
                raise RunnerTaskExecutionError(
                    "SimstackResult.files requires exactly one declared 'files' mapping"
                )
            for returned_file in result.files:
                if not isinstance(returned_file, Model):
                    raise RunnerTaskExecutionError(
                        "SimstackResult.files must contain SimStack document models"
                    )
            visible_models.append(
                (
                    "files",
                    _build_declared_value_model(
                        files_mappings[0],
                        {"elements": list(result.files)},
                        purpose="files result",
                    ),
                )
            )
        extra = getattr(result, "__pydantic_extra__", None) or {}
        for variable_name, value in extra.items():
            if not variable_name.startswith("_") and isinstance(value, Model):
                visible_models.append((variable_name, value))
        message = result.message
        result_info_files = list(result.info_files)
        infer_single_name = False
    elif isinstance(result, Model):
        raw_status = getattr(result, "status", None)
        raw_task_status = getattr(result, "task_status", None)
        declared_status = raw_status if raw_status is not None else raw_task_status
        if isinstance(declared_status, TaskStatus):
            declared_status = declared_status.value
        if declared_status not in (None, "", TaskStatus.COMPLETED.value):
            raise RunnerTaskExecutionError(
                f"Node result declares non-completed status: {declared_status}"
            )
        visible_models = [(result.__class__.__name__, result)]
        message = getattr(result, "message", None)
        result_info_files = []
        infer_single_name = True
    else:
        raise RunnerTaskExecutionError(
            f"Node returned unsupported type {type(result).__name__}"
        )

    visible_models = _declared_result_models(
        visible_models,
        result_mappings,
        infer_single_name=infer_single_name,
    )

    info_files: list[Model] = []
    seen_info_files: set[tuple[type[Model], str]] = set()
    for info_file in result_info_files + list(runner_info_files):
        if not isinstance(info_file, Model):
            raise RunnerTaskExecutionError(
                "Node info_files must contain SimStack document models"
            )
        file_key = (type(info_file), str(info_file.id))
        if file_key not in seen_info_files:
            seen_info_files.add(file_key)
            info_files.append(info_file)

    results = [
        _result_entry(model, variable_name=variable_name)
        for variable_name, model in visible_models
    ]
    info_payload = (
        {"elements": [_typed_document_entry(info_file) for info_file in info_files]}
        if info_files
        else None
    )
    return RunnerCommitPayload(
        results=results,
        info_files=info_payload,
        message=str(message) if message else None,
        return_kind=("bool" if isinstance(result, bool) else "multiple" if len(visible_models) != 1 else "model"),
        custom_name=result.custom_name if isinstance(result, SimstackResult) else None,
    )


def hydrate_task_results(task: Mapping[str, Any]) -> Model | SimstackResult | None:
    """Rebuild a completed child's return value from its hydrated result bundle."""

    hydrated = _hydrate_entries(task.get("results", []), kind="results")
    visible = [
        (variable_name, value)
        for variable_name, value in hydrated
        if not variable_name.startswith(_INTERNAL_RESULT_PREFIX)
    ]
    return_kind = task.get("return_kind")
    if not visible and return_kind != "multiple":
        return None
    if return_kind == "bool":
        if len(visible) != 1 or not isinstance(getattr(visible[0][1], "value", None), bool):
            raise RunnerTaskExecutionError("Completed boolean task has invalid result payload")
        return visible[0][1].value
    if len(visible) == 1 and return_kind != "multiple":
        return visible[0][1]
    info_payload = task.get("info_files") or {"elements": []}
    # Info documents have the same hydration contract without an exposed
    # variable name; assign internal local names, retaining typed dependencies.
    info_entries = [
        {**entry, "variable_name": f"info_file_{index}"}
        for index, entry in enumerate(info_payload["elements"])
    ]
    info_files = [value for _, value in _hydrate_entries(info_entries, kind="info files")]
    result = SimstackResult.from_task_metadata(
        {**task, "status": TaskStatus.COMPLETED},
        info_files=info_files,
    )
    for variable_name, value in visible:
        setattr(result, variable_name, value)
    return result


def cached_runner_commit(task: Mapping[str, Any], *, result_mappings: Sequence[Any]) -> RunnerCommitPayload:
    """Validate cached documents while preserving their immutable identities."""
    hydrated = _hydrate_entries(task.get("results", []), kind="cached results")
    _declared_result_models(hydrated, result_mappings, infer_single_name=False)
    if hydrate_task_results(task) is None:
        raise RunnerTaskExecutionError("Cached task has no valid return value")
    info = task.get("info_files") or {"elements": []}
    _hydrate_entries([
        {**entry, "variable_name": f"info_file_{index}"}
        for index, entry in enumerate(info["elements"])
    ], kind="cached info files")
    def committed(entry):
        return {key: value for key, value in entry.items() if key != "reference"}
    return RunnerCommitPayload(
        results=[committed(entry) for entry in task["results"]],
        info_files={"elements": [committed(entry) for entry in info["elements"]]},
        message=task.get("message"), return_kind=task["return_kind"], custom_name=None,
    )
