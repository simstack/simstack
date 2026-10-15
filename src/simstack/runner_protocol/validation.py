"""Small parsing helpers around the canonical Pydantic DTOs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from .dto import (
    RunnerActiveTasksV1,
    RunnerBootstrapV1,
    RunnerCapabilitiesV1,
    RunnerClaimEnvelopeV1,
    RunnerDocumentV1,
    RunnerLeaseResponseV1,
    RunnerModelMappingV1,
    RunnerNodeModelV1,
    RunnerResourceV1,
    RunnerTaskV1,
    RunnerTypedDocumentV1,
)
from .errors import RunnerProtocolError

DtoT = TypeVar("DtoT", bound=BaseModel)


def _parse(model: type[DtoT], payload: Any, *, operation: str) -> DtoT:
    try:
        encoded = json.dumps(payload, separators=(",", ":"))
        return model.model_validate_json(encoded, strict=True)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RunnerProtocolError(
            f"Runner {operation} does not match the exact v1 DTO: {exc}"
        ) from exc


def validate_attempt(attempt: Any) -> int:
    if type(attempt) is not int or attempt < 1:
        raise RunnerProtocolError("Runner task attempt must be an integer >= 1")
    return attempt


def validate_runner_resource_v1(
    resource: Any,
    *,
    operation: str,
) -> dict[str, Any]:
    return _parse(
        RunnerResourceV1, resource, operation=f"{operation} resource"
    ).model_dump(mode="json")


def validate_runner_mapping_tables_v1(
    model_mappings: Any,
    node_models: Any,
    *,
    operation: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        models = TypeAdapter(list[RunnerModelMappingV1]).validate_json(
            json.dumps(model_mappings, separators=(",", ":")), strict=True
        )
        nodes = TypeAdapter(list[RunnerNodeModelV1]).validate_json(
            json.dumps(node_models, separators=(",", ":")), strict=True
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise RunnerProtocolError(
            f"Runner {operation} mappings do not match the exact v1 DTO: {exc}"
        ) from exc
    return (
        [model.model_dump(mode="json") for model in models],
        [node.model_dump(mode="json") for node in nodes],
    )


def validate_runner_capabilities_v1(
    capabilities: Any,
    *,
    operation: str,
) -> tuple[int | None, int]:
    parsed = _parse(
        RunnerCapabilitiesV1,
        capabilities,
        operation=f"{operation} capabilities",
    )
    return parsed.max_runner_payload_bytes, parsed.max_inline_document_bytes


def validate_runner_bootstrap_v1(
    payload: Any,
    *,
    operation: str,
) -> dict[str, Any]:
    return _parse(RunnerBootstrapV1, payload, operation=operation).model_dump(
        mode="json"
    )


def validate_runner_document_entries_v1(
    entries: Any,
    *,
    operation: str,
    named: bool = True,
) -> None:
    model = list[RunnerDocumentV1] if named else list[RunnerTypedDocumentV1]
    try:
        TypeAdapter(model).validate_json(
            json.dumps(entries, separators=(",", ":")), strict=True
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise RunnerProtocolError(
            f"Runner {operation} does not match the exact v1 DTO: {exc}"
        ) from exc


def validate_runner_task_v1(
    task: Any,
    *,
    operation: str,
) -> dict[str, Any]:
    return _parse(RunnerTaskV1, task, operation=f"{operation} task").model_dump(
        mode="json"
    )


def validate_runner_claim_envelope_v1(
    payload: Any,
    *,
    operation: str,
) -> dict[str, Any]:
    return _parse(
        RunnerClaimEnvelopeV1, payload, operation=f"{operation} response"
    ).model_dump(mode="json")


def validate_runner_active_tasks_v1(
    payload: Any,
    *,
    operation: str,
) -> dict[str, Any]:
    return _parse(RunnerActiveTasksV1, payload, operation=operation).model_dump(
        mode="json"
    )


def validate_runner_lease_response_v1(
    payload: Any,
    *,
    claim_id: str,
    attempt: int,
    task_id: str,
) -> dict[str, Any]:
    parsed = _parse(
        RunnerLeaseResponseV1,
        payload,
        operation="lease renewal response",
    )
    if (
        parsed.task_id != task_id
        or parsed.claim_id != claim_id
        or parsed.attempt != attempt
    ):
        raise RunnerProtocolError("Runner lease renewal response is malformed")
    return cast(dict[str, Any], parsed.model_dump(mode="json"))


def exact_fields(
    payload: Mapping[str, Any],
    model: type[BaseModel],
    *,
    operation: str,
) -> None:
    """Projection-only field check derived from a canonical DTO model."""

    expected = frozenset(model.model_fields)
    received = set(payload)
    if received == expected:
        return
    missing = sorted(expected - received)
    unexpected = sorted(received - expected)
    details = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if unexpected:
        details.append("unexpected " + ", ".join(unexpected))
    raise RunnerProtocolError(
        f"Runner {operation} does not match the exact v1 DTO: " + "; ".join(details)
    )
