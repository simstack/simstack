from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from simstack.models import NodeRegistry, Parameters
from simstack.server_api import ServerAPI
from simstack.server_api.http_transport import RunnerAPIClient, RunnerAPIError


def task():
    entry = NodeRegistry(
        name="fixture",
        status="retrieved",
        function_hash="",
        arg_hash="input",
        func_mapping="fixture.run",
        parameters=Parameters(),
    )
    for name, value in {
        "generation": "generation",
        "claim_id": "claim",
        "start_id": str(uuid4()),
    }.items():
        object.__setattr__(entry, "_execution_" + name, value)
    return entry


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    transport = SimpleNamespace(
        server_url="https://example.test/api", token="resource-token", json=AsyncMock()
    )
    api = ServerAPI(transport, resource="test")
    api.requests.bootstrap_data = {"account_id": "test-account"}
    return api


@pytest.mark.asyncio
async def test_ambiguous_command_is_not_retried_and_explicit_replay_keeps_identity(api):
    identity = str(uuid4())
    api.requests.transport.json.side_effect = [
        RunnerAPIError("lost reply"),
        {"applied": True},
    ]
    with pytest.raises(RunnerAPIError):
        await api.requests.command(
            "/attempts/fail", {"error": "child failed"}, request_id=identity
        )
    api.requests.transport.json.assert_awaited_once()
    assert await api.requests.command(
        "/attempts/fail", {"error": "child failed"}, request_id=identity
    ) == {"applied": True}
    assert api.requests.transport.json.await_count == 2
    first, replay = api.requests.transport.json.await_args_list
    assert first == replay
    assert first.args[2]["request_id"] == identity


@pytest.mark.asyncio
async def test_acknowledged_start_removes_only_its_claim_receipt(api):
    from simstack.runner_runtime.launch_claims import receipt, finish

    entry = task()
    value = receipt(
        api, entry.id, begin=True, generation="generation", claim_id="claim"
    )
    finish(api, value, claimed=True)
    response = entry.model_dump(mode="json")
    response.update(
        {
            key: value
            for key, value in vars(entry).items()
            if key.startswith("_execution_")
        }
    )
    api.requests.transport.json.return_value = {"task": response}
    from simstack.runner_runtime.launch_claims import acknowledge_start

    started = await api.tasks.start_attempt(entry, process_id=100)
    acknowledge_start(api, entry, started)
    assert receipt(api, entry.id) is None
    body = api.requests.transport.json.call_args.args[2]
    assert body["request_id"] == entry._execution_start_id


@pytest.mark.asyncio
async def test_known_start_rejection_stops_ambiguous_write_retries(api):
    api.requests.transport.json.side_effect = [
        RunnerAPIError("write unavailable", status_code=503),
        {"state": "rejected", "status_code": 409, "detail": "original start was superseded"},
    ]
    with pytest.raises(RunnerAPIError, match="original start was superseded") as rejected:
        await api.tasks.start_attempt(task(), process_id=100)
    assert rejected.value.status_code == 409
    assert api.requests.transport.json.await_count == 2


@pytest.mark.asyncio
async def test_historical_outcome_keeps_original_attempt_identity(api):
    entry = task()
    api.requests.transport.json.return_value = {
        "task": None,
        "outcome": {
            "status": "failed",
            "error": "stored child error",
            "return_kind": "exception",
        },
    }
    outcome = await api.tasks.get_outcome(entry)
    assert outcome.error == "stored child error"
    assert outcome.status == "failed"
    assert api.tasks.execution(outcome) == api.tasks.execution(entry)


@pytest.mark.asyncio
async def test_publication_replay_is_not_authority_to_restart_live_child(api):
    original = task()
    original._execution_start_id = None
    wire = original.model_dump(mode="json")
    wire.update(
        {
            key: value
            for key, value in vars(original).items()
            if key.startswith("_execution_")
        }
    )
    current = {**wire, "status": "running", "_execution_start_id": str(uuid4())}
    api.requests.transport.json.side_effect = [
        {"state": "applied", "result": {"disposition": "launch_inline", "task": wire}},
        {"task": current},
    ]
    from simstack.runner_runtime.launch_claims import reconcile_claim

    result = await reconcile_claim(
        api,
        {
            "publication": {"parent_execution": {"task_id": "parent"}},
            "request_id": str(uuid4()),
        },
    )
    assert result == {"state": "active"}
    assert all(
        call.args[0] == "GET" for call in api.requests.transport.json.call_args_list
    )


def test_user_credentials_never_load_a_runner_token(monkeypatch):
    monkeypatch.setattr(
        "simstack.runner_runtime.runner_credentials.current_token",
        lambda *args: "runner-secret",
    )
    api = ServerAPI.for_user("https://example.test/api", "user-jwt")
    assert api.requests.token == "user-jwt"
    assert api.requests.transport.rotate_credentials is False
    with pytest.raises(ValueError, match="disable Runner credential rotation"):
        ServerAPI(
            RunnerAPIClient("https://example.test/api", "user-jwt"),
            authorization="user",
        )


@pytest.mark.asyncio
async def test_protocol_mismatch_fails_before_work_acquisition(api):
    api.requests.transport.json.return_value = {"protocol_version": 2}
    with pytest.raises(RunnerAPIError, match="protocol versions differ"):
        await api.resource.bootstrap()
    assert api.requests.transport.json.call_args.args[1] == "/runner/v3/bootstrap"
