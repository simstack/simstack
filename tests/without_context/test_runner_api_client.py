import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from simstack.models.parameters import Parameters
from simstack.runner_api import (
    ChildTaskState,
    ClaimedTask,
    ExecutionRunnerClient,
    ResolvedChildTask,
    RunnerApiConfigurationError,
    RunnerApiError,
    SupervisorRunnerClient,
)
from simstack.runner_api.file_transfer.transport import FileTransferTransport
from simstack.runner_protocol import (
    RUNNER_TASK_WIRE_FIELDS,
    RunnerProtocolError,
    validate_runner_claim_envelope_v1,
    validate_runner_mapping_tables_v1,
    validate_runner_task_v1,
)


def _task_bundle(task_id: str, **updates):
    task = {
        "task_schema_version": 1,
        "id": task_id,
        "name": "run",
        "status": "retrieved",
        "custom_name": None,
        "version": None,
        "workflow_package": None,
        "workflow_installation_id": None,
        "workflow_revision": None,
        "runtime_profile": None,
        "project": None,
        "category": None,
        "description": None,
        "call_path": None,
        "assignment_rule_id": None,
        "assignment_rule_name": None,
        "assignment_pattern": None,
        "error": None,
        "message": None,
        "return_kind": None,
        "parent_ids": [],
        "created_at": "2026-08-17T12:00:00Z",
        "started_at": None,
        "completed_at": None,
        "job_id": None,
        "function_hash": "function-hash",
        "arg_hash": "argument-hash",
        "func_mapping": "tests.run",
        "is_async": False,
        "parameters": Parameters().model_dump(mode="json"),
        "inputs": [],
        "results": [],
        "info_files": {"elements": []},
        "artifact_plan": {
            "required": False,
            "mappings": [],
            "child_artifacts": [],
        },
    }
    assert set(task) == RUNNER_TASK_WIRE_FIELDS
    task.update(updates)
    return task


def _bootstrap_payload(**updates):
    payload = {
        "api_version": "1",
        "server_time": "2026-08-17T12:00:00Z",
        "resource": {
            "resource_str": "local",
            "workdir": "/tmp/simstack-runner",
            "hostname": "runner-host",
            "python_paths": [],
            "environment_start": None,
            "is_default": True,
        },
        "allowed_resources": [],
        "model_mappings": [],
        "node_models": [],
        "scopes": [],
        "capabilities": {
            "max_runner_payload_bytes": 1000,
            "max_inline_document_bytes": 500,
        },
    }
    payload.update(updates)
    return payload


def _model_mapping_payload():
    return {
        "id": "507f1f77bcf86cd799439011",
        "name": "Input",
        "mapping": "tests.Input",
        "version": None,
        "json_schema": None,
        "ui_schema": None,
        "workflow_package": None,
        "workflow_installation_id": None,
        "workflow_revision": None,
        "runtime_profile": None,
    }


def _node_model_payload():
    return {
        "id": "507f1f77bcf86cd799439012",
        "name": "run_input",
        "function_mapping": "tests.run_input",
        "workflow_package": None,
        "workflow_installation_id": None,
        "workflow_revision": None,
        "runtime_profile": None,
        "version": None,
        "input_mappings": [
            {
                "name": "value",
                "mapping": "tests.Input",
                "description": "",
                "version": None,
            }
        ],
        "result_mappings": [],
        "called_nodes": [],
        "description": "",
        "favorite": False,
        "default_parameters": Parameters().model_dump(mode="json"),
    }


def _child_node_spec():
    return {
        "name": "child",
        "function_mapping": "tests.child",
        "function_hash": "function-hash",
        "arg_hash": "argument-hash",
        "call_path": ".child",
        "parameters": Parameters().model_dump(mode="json"),
        "custom_name": None,
        "category": None,
        "description": None,
        "version": None,
        "is_async": True,
    }


def test_mapping_table_dto_accepts_exact_v1_shape():
    model_mapping = _model_mapping_payload()
    node_model = _node_model_payload()

    models, nodes = validate_runner_mapping_tables_v1(
        [model_mapping], [node_model], operation="test"
    )

    assert models == [model_mapping]
    assert nodes == [node_model]


def test_runner_api_configuration_is_mandatory(monkeypatch):
    monkeypatch.delenv("SIMSTACK_SERVER_URL", raising=False)
    monkeypatch.delenv("SIMSTACK_RUNNER_TOKEN", raising=False)

    with pytest.raises(RunnerApiConfigurationError, match="SIMSTACK_SERVER_URL"):
        SupervisorRunnerClient.from_environment()


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        ("https://simstack.example", "/api/runner/bootstrap"),
        ("https://simstack.example/api", "/api/runner/bootstrap"),
        ("https://simstack.example/proxy", "/proxy/api/runner/bootstrap"),
        ("https://simstack.example/proxy/api", "/proxy/api/runner/bootstrap"),
    ],
)
def test_runner_api_path_normalization(server_url, expected):
    client = SupervisorRunnerClient(server_url=server_url, runner_token="token")

    assert client._transport.path("/bootstrap") == expected


def test_file_transfer_path_does_not_duplicate_api_prefix():
    transport = FileTransferTransport(
        server_url="https://simstack.example/api",
        token_provider=lambda: "token",
        timeout_seconds=60,
        retry_attempts=0,
        retry_backoff_seconds=0,
    )

    assert transport._path("/api/file-transfers") == "/api/file-transfers"


@pytest.mark.asyncio
async def test_claim_task_parses_lease_envelope(monkeypatch):
    client = SupervisorRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    request = AsyncMock(
        return_value={
            "claim_id": "claim-1",
            "attempt": 2,
            "lease_until": "2026-08-14T12:00:00Z",
            "execution_token": "execution-token-1",
            "execution_token_expires_at": "2026-09-14T12:00:00Z",
            "task": _task_bundle("task-1"),
        }
    )
    monkeypatch.setattr(client, "_request", request)

    claim = await client.claim_task(lease_seconds=90)

    assert claim == ClaimedTask(
        task=_task_bundle("task-1"),
        claim_id="claim-1",
        attempt=2,
        lease_until="2026-08-14T12:00:00Z",
        execution_token="execution-token-1",
        execution_token_expires_at="2026-09-14T12:00:00Z",
    )
    claim_request = request.await_args
    assert claim_request.args[:2] == ("POST", "/tasks/claim")
    assert claim_request.args[2]["lease_seconds"] == 90
    assert len(claim_request.args[2]["request_id"]) == 32
    assert claim_request.kwargs == {"retry_safe": True}


@pytest.mark.asyncio
async def test_claim_task_returns_none_for_204(monkeypatch):
    client = SupervisorRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    monkeypatch.setattr(client, "_request", AsyncMock(return_value=None))

    assert await client.claim_task() is None


@pytest.mark.asyncio
async def test_claim_request_id_is_reused_after_transport_response_loss(
    monkeypatch,
):
    client = SupervisorRunnerClient(
        server_url="https://simstack.example/api",
        runner_token="token",
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    lost_response = MagicMock()
    lost_response.request.side_effect = OSError("connection dropped")
    retry = MagicMock()
    retry.getresponse.return_value = SimpleNamespace(
        status=200,
        reason="OK",
        read=lambda: json.dumps(
            {
                "claim_id": "claim-1",
                "attempt": 1,
                "lease_until": "2026-08-17T12:00:00Z",
                "execution_token": "execution-token-1",
                "execution_token_expires_at": "2026-08-17T12:05:00Z",
                "task": _task_bundle("task-1"),
            }
        ).encode("utf-8"),
    )
    monkeypatch.setattr(
        client._transport,
        "connection",
        MagicMock(side_effect=[lost_response, retry]),
    )

    claim = await client.claim_task(lease_seconds=90)

    assert claim is not None
    first_body = json.loads(lost_response.request.call_args.kwargs["body"])
    retry_body = json.loads(retry.request.call_args.kwargs["body"])
    assert first_body["request_id"] == retry_body["request_id"]
    assert first_body["lease_seconds"] == 90


@pytest.mark.asyncio
async def test_get_task_uses_claim_and_attempt_query(monkeypatch):
    client = ExecutionRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    request = AsyncMock(
        return_value={
            "claim_id": "claim-1",
            "attempt": 3,
            "lease_until": "2026-08-14T12:00:00Z",
            "execution_token": "execution-token-1",
            "execution_token_expires_at": "2026-09-14T12:00:00Z",
            "task": _task_bundle("507f1f77bcf86cd799439011"),
        }
    )
    monkeypatch.setattr(client, "_request", request)

    task = await client.get_task(
        "507f1f77bcf86cd799439011", claim_id="claim-1", attempt=3
    )

    assert task.attempt == 3
    assert task.claim_id == "claim-1"
    request.assert_awaited_once_with(
        "GET",
        "/tasks/507f1f77bcf86cd799439011?claim_id=claim-1&attempt=3",
        retry_safe=True,
    )


@pytest.mark.asyncio
async def test_nested_task_contract(monkeypatch):
    client = ExecutionRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    request = AsyncMock(
        side_effect=[
            {
                "request_id": "request-1",
                "cache_hit": False,
                "child_id": "507f1f77bcf86cd799439012",
                "status": "submitted",
                "task": _task_bundle("507f1f77bcf86cd799439012"),
            },
            {
                "child_id": "507f1f77bcf86cd799439012",
                "status": "completed",
                "terminal": True,
                "task": _task_bundle("507f1f77bcf86cd799439012"),
            },
        ]
    )
    monkeypatch.setattr(client, "_request", request)

    resolved = await client.resolve_child(
        "507f1f77bcf86cd799439011",
        claim_id="claim-1",
        attempt=2,
        request_id="request-1",
        node=_child_node_spec(),
        inputs=[],
    )
    child = await client.get_child(
        "507f1f77bcf86cd799439011",
        resolved.child_id,
        claim_id="claim-1",
        attempt=2,
    )

    assert resolved == ResolvedChildTask(
        request_id="request-1",
        cache_hit=False,
        child_id="507f1f77bcf86cd799439012",
        status="submitted",
        task=_task_bundle("507f1f77bcf86cd799439012"),
    )
    assert child == ChildTaskState(
        child_id="507f1f77bcf86cd799439012",
        status="completed",
        terminal=True,
        task=_task_bundle("507f1f77bcf86cd799439012"),
    )
    child_request = request.await_args_list[0]
    assert child_request.args[:2] == (
        "POST",
        "/tasks/507f1f77bcf86cd799439011/children/resolve",
    )
    assert child_request.args[2]["claim_id"] == "claim-1"
    assert child_request.args[2]["attempt"] == 2
    assert child_request.args[2]["request_id"] == "request-1"
    assert child_request.args[2]["node"]["function_mapping"] == "tests.child"
    assert child_request.args[2]["inputs"] == []
    assert request.await_args_list[0].kwargs == {"retry_safe": True}
    assert request.await_args_list[1].args == (
        "GET",
        "/tasks/507f1f77bcf86cd799439011/children/507f1f77bcf86cd799439012?claim_id=claim-1&attempt=2",
    )
    assert request.await_args_list[1].kwargs == {"retry_safe": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (("request_id", 1), ("child_id", {}), ("status", 1)),
)
async def test_child_resolution_rejects_string_coercion(
    monkeypatch, field, invalid_value
):
    client = ExecutionRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    payload = {
        "request_id": "request-1",
        "cache_hit": False,
        "child_id": "child-1",
        "status": "submitted",
        "task": _task_bundle("child-1"),
    }
    payload[field] = invalid_value
    monkeypatch.setattr(client, "_request", AsyncMock(return_value=payload))

    with pytest.raises(RunnerApiError, match="exact v1 DTO"):
        await client.resolve_child(
            "parent-1",
            claim_id="claim-1",
            attempt=1,
            request_id="request-1",
            node=_child_node_spec(),
            inputs=[],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "invalid_value"), (("child_id", 1), ("status", {})))
async def test_child_status_rejects_string_coercion(monkeypatch, field, invalid_value):
    client = ExecutionRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    payload = {
        "child_id": "child-1",
        "status": "completed",
        "terminal": True,
        "task": _task_bundle("child-1"),
    }
    payload[field] = invalid_value
    monkeypatch.setattr(client, "_request", AsyncMock(return_value=payload))

    with pytest.raises(RunnerApiError, match="exact v1 DTO"):
        await client.get_child(
            "parent-1",
            "child-1",
            claim_id="claim-1",
            attempt=1,
        )


@pytest.mark.asyncio
async def test_task_mutations_bind_claim_attempt_in_every_body(monkeypatch):
    client = ExecutionRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    request = AsyncMock(
        side_effect=[
            {
                "task_id": "507f1f77bcf86cd799439011",
                "claim_id": "claim-1",
                "attempt": 7,
                "lease_until": "2026-08-14T12:00:00Z",
                "execution_token": "execution-token-2",
                "execution_token_expires_at": "2026-09-14T12:00:00Z",
            },
            {
                "task_id": "507f1f77bcf86cd799439011",
                "status": "running",
                "accepted": True,
            },
            {
                "task_id": "507f1f77bcf86cd799439011",
                "status": "completed",
                "accepted": True,
                "result_references": [],
            },
            {
                "task_id": "507f1f77bcf86cd799439011",
                "status": "failed",
                "accepted": True,
            },
        ]
    )
    monkeypatch.setattr(client, "_request", request)
    task_id = "507f1f77bcf86cd799439011"

    await client.renew_lease(task_id, claim_id="claim-1", attempt=7, lease_seconds=90)
    await client.update_task_status(
        task_id,
        claim_id="claim-1",
        attempt=7,
        expected_status="retrieved",
        status="running",
        observed_at="2026-08-14T11:59:00+00:00",
    )
    await client.commit_task(
        task_id,
        claim_id="claim-1",
        attempt=7,
        expected_status="running",
        results=[],
    )
    await client.fail_task(
        task_id,
        claim_id="claim-1",
        attempt=7,
        error="WorkerError",
        message="failed",
    )

    assert request.await_args_list[0].args == (
        "POST",
        f"/tasks/{task_id}/lease",
        {"claim_id": "claim-1", "attempt": 7, "lease_seconds": 90},
    )
    assert request.await_args_list[0].kwargs == {"retry_safe": True}
    assert request.await_args_list[1].args == (
        "POST",
        f"/tasks/{task_id}/status",
        {
            "claim_id": "claim-1",
            "attempt": 7,
            "expected_status": "retrieved",
            "status": "running",
            "observed_at": "2026-08-14T11:59:00+00:00",
        },
    )
    assert request.await_args_list[1].kwargs == {"retry_safe": True}
    assert request.await_args_list[2].args == (
        "POST",
        f"/tasks/{task_id}/commit",
        {
            "claim_id": "claim-1",
            "attempt": 7,
            "expected_status": "running",
            "results": [],
            "info_files": None,
            "message": None,
            "return_kind": None,
            "custom_name": None,
            "expected_custom_name": None,
        },
    )
    assert request.await_args_list[2].kwargs == {"retry_safe": True}
    assert request.await_args_list[3].args == (
        "POST",
        f"/tasks/{task_id}/fail",
        {
            "claim_id": "claim-1",
            "attempt": 7,
            "error": "WorkerError",
            "message": "failed",
        },
    )


@pytest.mark.asyncio
async def test_task_mutations_reject_invalid_attempt_before_request(monkeypatch):
    client = ExecutionRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    request = AsyncMock()
    monkeypatch.setattr(client, "_request", request)

    with pytest.raises(RunnerApiError, match=">= 1"):
        await client.fail_task(
            "507f1f77bcf86cd799439011",
            claim_id="claim-1",
            attempt=0,
            error="WorkerError",
        )

    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_key", (None, "_id"))
@pytest.mark.parametrize("document_kind", ("result", "info_file"))
async def test_commit_rejects_documents_without_canonical_wire_identity(
    monkeypatch, wire_key, document_kind
):
    client = ExecutionRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    request = AsyncMock()
    monkeypatch.setattr(client, "_request", request)
    value = {"payload": 1}
    if wire_key is not None:
        value[wire_key] = "document-1"
    document = {
        "variable_mapping": "tests.Output",
        "value": value,
        "dependencies": [],
    }
    results = []
    info_files = None
    if document_kind == "result":
        results = [{"variable_name": "value", **document}]
    else:
        info_files = {"elements": [document]}

    with pytest.raises(RunnerApiError, match="exact v1 DTO"):
        await client.commit_task(
            "task-1",
            claim_id="claim-1",
            attempt=1,
            results=results,
            info_files=info_files,
        )

    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_token", 123),
        ("execution_token", " token "),
        ("execution_token_expires_at", 123),
    ],
)
async def test_lease_renewal_rejects_coerced_token_fields(monkeypatch, field, value):
    client = ExecutionRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    payload = {
        "task_id": "task-1",
        "claim_id": "claim-1",
        "attempt": 1,
        "lease_until": "2026-08-17T12:00:00Z",
        "execution_token": "execution-token-1",
        "execution_token_expires_at": "2026-08-17T12:05:00Z",
    }
    payload[field] = value
    monkeypatch.setattr(client, "_request", AsyncMock(return_value=payload))

    with pytest.raises(RunnerApiError, match="exact v1 DTO"):
        await client.renew_lease("task-1", claim_id="claim-1", attempt=1)


@pytest.mark.asyncio
async def test_bootstrap_rejects_wrong_api_version(monkeypatch):
    client = SupervisorRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    monkeypatch.setattr(
        client,
        "_request",
        AsyncMock(return_value=_bootstrap_payload(api_version="2")),
    )

    with pytest.raises(RunnerApiError, match="exact v1 DTO"):
        await client.bootstrap()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("missing", "extra"))
async def test_bootstrap_rejects_protocol_drift(monkeypatch, mutation):
    client = SupervisorRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    payload = _bootstrap_payload()
    if mutation == "missing":
        payload.pop("server_time")
    else:
        payload["legacy_database"] = {}
    monkeypatch.setattr(client, "_request", AsyncMock(return_value=payload))

    with pytest.raises(RunnerApiError, match="exact v1 DTO"):
        await client.bootstrap()


@pytest.mark.parametrize(
    "mutation",
    (
        "legacy_id",
        "missing_id",
        "extra",
        "coerced_id",
        "coerced_version",
    ),
)
def test_model_mapping_dto_rejects_legacy_or_coerced_shape(mutation):
    mapping = _model_mapping_payload()
    if mutation == "legacy_id":
        mapping["_id"] = mapping.pop("id")
    elif mutation == "missing_id":
        mapping.pop("id")
    elif mutation == "extra":
        mapping["legacy_schema"] = {}
    elif mutation == "coerced_id":
        mapping["id"] = 1
    else:
        mapping["version"] = 1

    with pytest.raises(RunnerProtocolError):
        validate_runner_mapping_tables_v1(
            [mapping], [_node_model_payload()], operation="test"
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "legacy_pickle",
        "missing_id",
        "extra",
        "coerced_id",
        "coerced_favorite",
        "coerced_called_node",
        "coerced_parameters",
        "coerced_bool_parameter",
        "legacy_parameters",
        "nested_extra",
        "nested_coercion",
    ),
)
def test_node_model_dto_rejects_legacy_or_coerced_shape(mutation):
    node = _node_model_payload()
    if mutation == "legacy_pickle":
        node["pickle_function"] = None
    elif mutation == "missing_id":
        node.pop("id")
    elif mutation == "extra":
        node["legacy_node"] = {}
    elif mutation == "coerced_id":
        node["id"] = 1
    elif mutation == "coerced_favorite":
        node["favorite"] = 1
    elif mutation == "coerced_called_node":
        node["called_nodes"] = [1]
    elif mutation == "coerced_parameters":
        node["default_parameters"] = []
    elif mutation == "coerced_bool_parameter":
        node["default_parameters"]["force_rerun"] = 1
    elif mutation == "legacy_parameters":
        parameters = node["default_parameters"]
        parameters["slurm_parameters_data"] = parameters.pop("slurm_parameters")
    elif mutation == "nested_extra":
        node["input_mappings"][0]["legacy_mapping"] = None
    else:
        node["input_mappings"][0]["mapping"] = 1

    with pytest.raises(RunnerProtocolError):
        validate_runner_mapping_tables_v1(
            [_model_mapping_payload()], [node], operation="test"
        )


@pytest.mark.parametrize(
    ("mutation", "message"), [("missing", "missing"), ("extra", "Extra")]
)
def test_task_bundle_rejects_protocol_drift(mutation, message):
    task = _task_bundle("507f1f77bcf86cd799439011")
    if mutation == "missing":
        task.pop("artifact_plan")
    else:
        task["legacy_node"] = {}

    with pytest.raises(RunnerProtocolError, match=message):
        validate_runner_task_v1(task, operation="test")


@pytest.mark.parametrize("invalid_id", ["", " ", " task-1 ", None])
def test_task_bundle_requires_canonical_opaque_id(invalid_id):
    task = _task_bundle(invalid_id)

    with pytest.raises(RunnerProtocolError):
        validate_runner_task_v1(task, operation="test")


@pytest.mark.parametrize(
    "mutation",
    ("missing_reference", "missing_value_id", "null_dependencies", "extra_field"),
)
def test_task_bundle_rejects_noncanonical_nested_document(mutation):
    task = _task_bundle("task-1")
    entry = {
        "variable_name": "value",
        "variable_mapping": "tests.Input",
        "reference": "input-id",
        "value": {"id": "input-id", "value": 1},
        "dependencies": [],
    }
    if mutation == "missing_reference":
        entry.pop("reference")
    elif mutation == "missing_value_id":
        entry["value"].pop("id")
    elif mutation == "null_dependencies":
        entry["dependencies"] = None
    else:
        entry["legacy_extra"] = {}
    task["inputs"] = [entry]

    with pytest.raises(RunnerProtocolError):
        validate_runner_task_v1(task, operation="test")


@pytest.mark.parametrize("invalid_attempt", [True, "2", 0])
def test_claim_envelope_rejects_coerced_or_invalid_attempt(invalid_attempt):
    payload = {
        "claim_id": "claim-1",
        "attempt": invalid_attempt,
        "lease_until": "2026-08-17T12:00:00Z",
        "execution_token": "execution-token-1",
        "execution_token_expires_at": "2026-08-17T12:05:00Z",
        "task": _task_bundle("task-1"),
    }

    with pytest.raises(RunnerProtocolError):
        validate_runner_claim_envelope_v1(payload, operation="test")


def test_outgoing_payload_honors_server_advertised_capacity(monkeypatch):
    client = SupervisorRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )
    client._transport.max_payload_bytes = 10
    connection = MagicMock()
    monkeypatch.setattr(client._transport, "connection", connection)

    with pytest.raises(RunnerApiError, match="advertised payload capacity"):
        client._transport.request("POST", "/logs/batch", {"payload": "too large"})

    connection.assert_not_called()


def test_explicit_blank_credentials_never_fall_back_to_environment(monkeypatch):
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://ambient.example/api")
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "ambient-token")

    with pytest.raises(RunnerApiConfigurationError, match="SIMSTACK_SERVER_URL"):
        SupervisorRunnerClient.from_environment(server_url="", runner_token="token")
    with pytest.raises(RunnerApiConfigurationError, match="SIMSTACK_RUNNER_TOKEN"):
        SupervisorRunnerClient.from_environment(
            server_url="https://simstack.example/api", runner_token=""
        )


@pytest.mark.asyncio
async def test_log_batch_requires_content():
    client = SupervisorRunnerClient(
        server_url="https://simstack.example/api", runner_token="token"
    )

    with pytest.raises(RunnerApiError, match="At least one"):
        await client.send_logs()
