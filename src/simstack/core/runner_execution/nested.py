"""Claim-scoped nested node resolution over the execution API."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from simstack.core.context import context
from simstack.core.node_code_version import node_code_version
from simstack.domain.execution_parameters import (
    inherit_parent_slurm_parameters_for_self,
)
from simstack.models.document import Model, encode_document_value
from simstack.models.parameters import Parameters
from simstack.runner_protocol import (
    RUNNER_CHILD_PENDING_STATUSES,
    RUNNER_TERMINAL_SUCCESS_STATUS,
)

from .config import child_poll_seconds
from .errors import RunnerTaskExecutionError, RunnerTaskPayloadError
from .mappings import _effective_node_version, _mapping_for_model
from .serialization import _result_entry, hydrate_task_results
from .state import current_runner_claim


def _nested_input_values(
    function: Callable[..., Any],
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> list[tuple[str, Model]]:
    signature = inspect.signature(function)
    parameter_names = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    user_kwargs = {
        name: value for name, value in kwargs.items() if name in parameter_names
    }
    try:
        bound = signature.bind_partial(*args, **user_kwargs)
    except TypeError as exc:
        raise RunnerTaskExecutionError(
            f"Could not bind nested node {function.__name__} inputs"
        ) from exc

    values: list[tuple[str, Model]] = []
    for name in signature.parameters:
        if name not in bound.arguments:
            continue
        value = bound.arguments[name]
        if not isinstance(value, Model):
            raise RunnerTaskExecutionError(
                f"Nested node input {name!r} is not a SimStack document model"
            )
        values.append((name, value))
    return values


def _validate_input_contract(
    values: Sequence[tuple[str, Model]],
    declared_mappings: Sequence[Any],
    *,
    kind: str,
) -> None:
    declared = Counter(
        (str(mapping.name), str(mapping.mapping)) for mapping in declared_mappings
    )
    for variable_name, model in values:
        mapping = _mapping_for_model(model)
        pair = (variable_name, str(mapping.mapping))
        if declared[pair] < 1:
            raise RunnerTaskExecutionError(
                f"{kind} input is absent from its bootstrap contract: "
                f"({pair[0]!r}, {pair[1]!r})"
            )
        declared[pair] -= 1
    missing = [pair for pair, count in declared.items() if count > 0]
    if missing:
        raise RunnerTaskExecutionError(
            f"{kind} omitted declared inputs: "
            + ", ".join(f"({name!r}, {mapping!r})" for name, mapping in missing)
        )


def _root_call_arguments(
    function: Callable[..., Any],
    values: Sequence[tuple[str, Model]],
) -> tuple[list[Model], dict[str, Model]]:
    signature = inspect.signature(function)
    value_by_name = dict(values)
    if len(value_by_name) != len(values):
        raise RunnerTaskPayloadError("Runner task has duplicate input names")

    positional: list[Model] = []
    keyword: dict[str, Model] = {}
    accepts_extra_names = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    for name, value in values:
        parameter = signature.parameters.get(name)
        if parameter is None:
            if not accepts_extra_names:
                raise RunnerTaskPayloadError(
                    f"Runner task input {name!r} is absent from the node signature"
                )
            keyword[name] = value
        elif parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        elif parameter.kind in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            keyword[name] = value
        else:
            raise RunnerTaskPayloadError(
                f"Runner task input {name!r} cannot bind to {parameter.kind.name}"
            )
    return positional, keyword


async def execute_runner_nested_node(
    function: Callable[..., Any],
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    *,
    version: str | None,
) -> Any:
    """Resolve and await a decorated child through the claim-scoped API."""

    claim = current_runner_claim()
    mappings = context.node_mappings
    if mappings is None:
        raise RunnerTaskPayloadError("Runner bootstrap has no node mappings")
    candidate_mapping = f"{function.__module__}.{function.__name__}"
    node_model = mappings.get_by_mapping(candidate_mapping)
    if node_model is None:
        raise RunnerTaskPayloadError(
            f"Nested node {function.__name__!r} is absent from runner bootstrap"
        )

    input_values = _nested_input_values(function, args, kwargs)
    _validate_input_contract(
        input_values,
        node_model.input_mappings,
        kind="Nested node",
    )
    # Import lazily to avoid a module cycle: node.py calls this helper from its wrappers.
    from simstack.core.node import compute_arg_hash

    from .blobs import publish_runtime_blobs

    await asyncio.to_thread(publish_runtime_blobs, [value for _, value in input_values])

    arg_hash = compute_arg_hash([value for _, value in input_values])
    function_hash = node_code_version(
        function, workflow_revision=getattr(node_model, "workflow_revision", None)
    )
    function_mapping = str(node_model.function_mapping)
    pinned_version = _effective_node_version(function, node_model)
    if getattr(node_model, "workflow_revision", None) is not None:
        if version is not None and version != pinned_version:
            raise RunnerTaskPayloadError("Nested node decorator version differs from its pinned workflow snapshot")
        requested_version = pinned_version
    else:
        requested_version = version or getattr(node_model, "version", None)
    call_path = str(kwargs.get("call_path") or f".{function.__name__}")
    parameters = kwargs.get("parameters")
    if not isinstance(parameters, BaseModel):
        raise RunnerTaskExecutionError(
            f"Nested node {function.__name__} has invalid parameters"
        )
    parent_parameters = kwargs.get("parent_parameters")
    if (
        isinstance(parameters, Parameters)
        and isinstance(parent_parameters, Parameters)
        and parameters.resource == "self"
    ):
        if "in_docker" not in parameters.model_fields_set:
            parameters.in_docker = parent_parameters.in_docker
        if "queue" not in parameters.model_fields_set:
            parameters.queue = parent_parameters.queue
        inherit_parent_slurm_parameters_for_self(parameters, parent_parameters)

    # Idempotency belongs to this logical invocation, not the semantic cache
    # key: two calls can share code/arguments while carrying distinct names or
    # fresh immutable input ids. Transport retries reuse this exact request.
    request_id = uuid.uuid4().hex
    spec = {
        "name": str(node_model.name),
        "function_mapping": function_mapping,
        "function_hash": function_hash,
        "arg_hash": arg_hash,
        "call_path": call_path,
        "parameters": encode_document_value(
            parameters.model_dump(mode="python", by_alias=True)
        ),
        "custom_name": kwargs.get("custom_name"),
        "category": getattr(node_model, "category", None),
        "description": getattr(node_model, "description", None),
        "version": requested_version,
        "is_async": asyncio.iscoroutinefunction(function),
    }
    inputs = [
        _result_entry(value, variable_name=variable_name)
        for variable_name, value in input_values
    ]
    resolved = await context.runner_api.resolve_child(
        claim.task_id,
        claim_id=claim.claim_id,
        attempt=claim.attempt,
        request_id=request_id,
        node=spec,
        inputs=inputs,
        prefer_inline=str(parameters.resource) == "self",
    )

    status = resolved.status
    task = resolved.task
    if resolved.inline_claim is not None:
        from .inline_child import execute_inline_child

        await execute_inline_child(resolved.inline_claim)
        child = await context.runner_api.get_child(
            claim.task_id,
            resolved.child_id,
            claim_id=claim.claim_id,
            attempt=claim.attempt,
        )
        if child.status != RUNNER_TERMINAL_SUCCESS_STATUS:
            raise RunnerTaskExecutionError(
                f"Inline child {child.child_id} failed: {child.task.get('error') or child.status}"
            )
        return hydrate_task_results(child.task)
    if status == RUNNER_TERMINAL_SUCCESS_STATUS:
        return hydrate_task_results(task)
    if status not in RUNNER_CHILD_PENDING_STATUSES:
        error = task.get("error") or task.get("message") or status
        raise RunnerTaskExecutionError(
            f"Nested task {resolved.child_id} terminated with {status}: {error}"
        )

    poll_seconds = child_poll_seconds()
    while True:
        await asyncio.sleep(poll_seconds)
        child = await context.runner_api.get_child(
            claim.task_id,
            resolved.child_id,
            claim_id=claim.claim_id,
            attempt=claim.attempt,
        )
        if not child.terminal:
            continue
        if child.status == RUNNER_TERMINAL_SUCCESS_STATUS:
            return hydrate_task_results(child.task)
        error = child.task.get("error") or child.task.get("message") or child.status
        raise RunnerTaskExecutionError(
            f"Nested task {child.child_id} terminated with {child.status}: {error}"
        )
