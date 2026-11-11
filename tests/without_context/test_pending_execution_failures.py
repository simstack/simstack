import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from simstack.models.document import ObjectId
from simstack.runner_runtime.pending_execution_failures import record_failure, reconcile_failures
from simstack.server_api.http_transport import RunnerAPIError


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("state", ["active", "terminating", "terminal", "stale", "outage"])
async def test_cleanup_guard_replays_only_append_only_attempts(tmp_path, monkeypatch, restart, state):
    from simstack.runner_runtime.pending_execution_failures import record_observed_failure, reconcile_observed_failure
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    server, attempt = client(), execution()
    pending = record_observed_failure("physical exit", phase="subprocess", execution=attempt,
        server=server, exit_code=17, cleanup={"container": True})
    calls = []
    async def reconcile(*, claims, outcomes, request_id=None):
        calls.append((claims, outcomes, request_id))
        if state == "outage":
            raise RunnerAPIError("offline", status_code=503)
        if claims:
            return {"claims": [{"execution": attempt, "state": "active" if state == "terminating" else state,
                "task": SimpleNamespace(status="terminating" if state == "terminating" else "running")}]}
        assert "cleanup" not in outcomes[0]
        assert outcomes[0]["execution"] == attempt and outcomes[0]["exit_code"] == 17
        assert request_id == pending[1]["request_id"]
        return {"outcomes": [{"execution": attempt, "state": state}]}
    server.tasks.reconcile_attempts = reconcile
    if restart:
        await reconcile_failures(server)
    else:
        accepted = await reconcile_observed_failure(server, pending)
        assert accepted == (state not in {"active", "outage"})
    assert pending[0].exists() == (state in {"active", "outage"})
    assert sum(bool(outcomes) for _, outcomes, _ in calls) == int(state not in {"active", "outage"})


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("restart", [False, True])
async def test_guarded_native_receipt_requires_stopped_original_group(tmp_path, monkeypatch, stopped, restart):
    from simstack.runner_runtime.pending_execution_failures import cleanup_scope, record_observed_failure, reconcile_observed_failure
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    server, attempt = client(), execution()
    guard = {"process_group": 12345, "scope": cleanup_scope()}
    checked = []
    def observe(value):
        checked.append(value)
        return stopped
    monkeypatch.setattr("simstack.runner_runtime.native_observations._science_stopped", observe)
    pending = record_observed_failure("SIGKILL", phase="subprocess", execution=attempt,
        server=server, signal=9, cleanup=guard)
    if restart:
        await reconcile_failures(server)
    else:
        assert await reconcile_observed_failure(server, pending) is stopped
    assert checked == [guard]
    assert pending[0].exists() is not stopped
    calls = server.tasks.reconcile_attempts.await_args_list
    assert sum(bool(call.kwargs["outcomes"]) for call in calls) == int(stopped)
    if stopped:
        outcome, = calls[-1].kwargs["outcomes"]
        assert outcome["signal"] == 9 and "cleanup" not in outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("scope", [None, {"hostname": "another-host"}, {"hostname": "same-host", "pid_namespace": "another-namespace"}])
async def test_foreign_or_unknown_pid_scope_cannot_release_cleanup_guard(tmp_path, monkeypatch, restart, scope):
    from simstack.runner_runtime.pending_execution_failures import record_observed_failure, reconcile_observed_failure
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    monkeypatch.setattr("simstack.runner_runtime.pending_execution_failures.cleanup_scope",
        lambda: {"hostname": "same-host", "pid_namespace": "original-namespace"})
    def forbidden(_):
        pytest.fail("A foreign or unverified namespace cannot supply local stop proof")
    monkeypatch.setattr("simstack.runner_runtime.native_observations._science_stopped", forbidden)
    server = client()
    pending = record_observed_failure("physical exit", phase="subprocess", execution=execution(),
        server=server, exit_code=17, cleanup={"process_group": 12345, "scope": scope})
    if restart:
        await reconcile_failures(server)
    else:
        assert not await reconcile_observed_failure(server, pending)
    assert pending[0].exists()
    assert all(not call.kwargs["outcomes"] for call in server.tasks.reconcile_attempts.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_unavailable_native_visibility_retains_or_appends_without_stop_ack(tmp_path, monkeypatch, terminal):
    from simstack.runner_runtime.pending_execution_failures import cleanup_scope, record_observed_failure, reconcile_observed_failure
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    def unavailable(_):
        raise PermissionError("process table inaccessible")
    monkeypatch.setattr("simstack.runner_runtime.native_observations._science_stopped", unavailable)
    server, attempt = client(), execution()
    server.tasks.reconcile_attempts.side_effect = [
        {"claims": [{"execution": attempt, "state": "terminal" if terminal else "active"}]},
        {"outcomes": []},
    ]
    pending = record_observed_failure("SIGKILL", phase="subprocess", execution=attempt,
        server=server, signal=9, cleanup={"process_group": 12345, "scope": cleanup_scope()})
    assert await reconcile_observed_failure(server, pending) is terminal
    assert pending[0].exists() is not terminal
    if terminal:
        assert "execution_stopped" not in server.tasks.reconcile_attempts.await_args.kwargs["outcomes"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["completed", "failed", "timeout", "terminating", "active", "foreign", "missing", "read-denied", "read-outage"])
async def test_retired_local_cleanup_guard_requires_exact_authorized_terminal_history(tmp_path, monkeypatch, case):
    from simstack.runner_runtime.pending_execution_failures import record_observed_failure, reconcile_observed_failure
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    server, attempt = client("local"), execution()
    accepted = case in {"completed", "failed", "timeout", "terminating"}
    recorded = {"execution": attempt, "status": case if accepted else "running"}
    if case == "foreign":
        recorded = {"execution": {**attempt, "generation": "new-generation"}, "status": "completed"}
    if case == "missing":
        recorded = {"status": "completed"}
    server.requests.read = AsyncMock(return_value={"outcome": recorded}, side_effect=
        RunnerAPIError("history denied", status_code=403 if case == "read-denied" else 503)
        if case in {"read-denied", "read-outage"} else None)
    server.tasks.reconcile_attempts.side_effect = [RunnerAPIError("active local claim denied", status_code=403), {"outcomes": []}]
    pending = record_observed_failure("late physical exit", phase="subprocess", execution=attempt,
        server=server, exit_code=17, cleanup={"container": True})
    assert await reconcile_observed_failure(server, pending) is accepted
    server.requests.read.assert_awaited_once_with(f"/tasks/{attempt['task_id']}/outcome",
        **{key: value for key, value in attempt.items() if key != "task_id"})
    assert pending[0].exists() is not accepted
    assert sum(bool(call.kwargs["outcomes"]) for call in server.tasks.reconcile_attempts.await_args_list) == int(accepted)


@pytest.mark.asyncio
@pytest.mark.parametrize("role,status", [("runner", 403), ("local", 401), ("local", 503)])
async def test_cleanup_history_fallback_does_not_expand_other_authority(tmp_path, monkeypatch, role, status):
    from simstack.runner_runtime.pending_execution_failures import record_observed_failure, reconcile_observed_failure
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    server = client(role)
    server.requests.read = AsyncMock()
    server.tasks.reconcile_attempts.side_effect = RunnerAPIError("claim denied", status_code=status)
    pending = record_observed_failure("late exit", phase="subprocess", execution=execution(),
        server=server, exit_code=17, cleanup={"container": True})
    assert not await reconcile_observed_failure(server, pending)
    server.requests.read.assert_not_awaited()
    assert pending[0].exists()


def client(role="runner", *, resource="cluster", url="https://server.example/api", token="resource-token", account_id="test-account"):
    return SimpleNamespace(requests=SimpleNamespace(server_url=url, authorization=role, resource=resource, token=token,
        bootstrap_data={"account_id": account_id}),
        tasks=SimpleNamespace(reconcile_attempts=AsyncMock(return_value={"claims": []})))


def execution():
    return {"task_id": str(ObjectId()), "generation": str(uuid4()), "claim_id": str(uuid4()), "start_id": str(uuid4())}


@pytest.mark.asyncio
async def test_foreign_local_failure_cannot_block_managed_runner_reconciliation(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    local, runner = client("local", token="task-scoped-secret"), client()
    local_execution, managed_execution = execution(), execution()
    path = record_failure("local bootstrap failed", phase="bootstrap", execution=local_execution, server=local)
    foreign = tmp_path / "000-local.json"
    path.rename(foreign)
    managed = record_failure("managed bootstrap failed", phase="bootstrap", execution=managed_execution, server=runner)
    await reconcile_failures(runner)
    runner.tasks.reconcile_attempts.assert_awaited_once()
    assert runner.tasks.reconcile_attempts.await_args.kwargs["outcomes"][0]["execution"] == managed_execution
    assert foreign.is_file() and not managed.exists()
    assert "task-scoped-secret" not in foreign.read_text()
    await reconcile_failures(local)
    assert local.tasks.reconcile_attempts.await_args.kwargs["outcomes"][0]["execution"] == local_execution
    assert not foreign.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [
    client("local", token="another-task"),
    client(resource="other-cluster"),
    client(url="https://other-server.example/api"),
])
async def test_other_execution_server_or_resource_keeps_its_failure_pending(tmp_path, monkeypatch, foreign):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    owner = client("local", token="original-task")
    path = record_failure("original failure", phase="execute", execution=execution(), server=owner)
    await reconcile_failures(foreign)
    foreign.tasks.reconcile_attempts.assert_not_awaited()
    assert path.is_file()
    await reconcile_failures(owner)
    owner.tasks.reconcile_attempts.assert_awaited_once()
    assert not path.exists()


@pytest.mark.asyncio
async def test_unknown_old_and_corrupt_journals_are_retained_without_blocking_valid_record(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    old, broken = tmp_path / "000-old.json", tmp_path / "001-broken.json"
    old.write_text(json.dumps({"request_id": str(uuid4()), "execution": execution(), "error": "old format"}))
    broken.write_text("interrupted file")
    runner = client()
    valid = record_failure("valid current format", phase="execute", execution=execution(), server=runner)
    await reconcile_failures(runner)
    assert old.exists() and broken.exists() and not valid.exists()
    runner.tasks.reconcile_attempts.assert_awaited_once()
    assert "unknown execution failure journal" in caplog.text
    assert "Unreadable execution failure journal" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 503])
async def test_own_authorization_or_dependency_error_is_not_swallowed(tmp_path, monkeypatch, status):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    runner = client()
    path = record_failure("must remain recoverable", phase="execute", execution=execution(), server=runner)
    runner.tasks.reconcile_attempts.side_effect = RunnerAPIError("unavailable", status_code=status)
    with pytest.raises(RunnerAPIError):
        await reconcile_failures(runner)
    assert path.exists()


@pytest.mark.asyncio
async def test_accounts_sharing_resource_and_directory_cannot_consume_each_others_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    owner = client(account_id="account-A")
    other = client(account_id="account-B")
    path = record_failure("owner's bootstrap failure", phase="bootstrap", execution=execution(), server=owner)
    await reconcile_failures(other)
    other.tasks.reconcile_attempts.assert_not_awaited()
    assert path.is_file()
    # Account identity survives normal resource-token rotation.
    owner.requests.token = "rotated-resource-token"
    await reconcile_failures(owner)
    owner.tasks.reconcile_attempts.assert_awaited_once()
    assert not path.exists()


@pytest.mark.asyncio
async def test_prebootstrap_account_scope_survives_credential_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://server.example/api")
    monkeypatch.setenv("SIMSTACK_EXECUTION_RESOURCE", "cluster")
    monkeypatch.setenv("SIMSTACK_EXECUTION_ACCOUNT_ID", "account-A")
    monkeypatch.delenv("SIMSTACK_LOCAL_EXECUTION_TOKEN", raising=False)
    path = record_failure("failed before initialization", phase="bootstrap", execution=execution(), exit_code=17)
    await reconcile_failures(client(account_id="account-B"))
    assert path.exists()
    owner = client(account_id="account-A", token="new-token")
    await reconcile_failures(owner)
    assert not path.exists()
    payload = owner.tasks.reconcile_attempts.await_args.kwargs["outcomes"][0]
    assert payload["exit_code"] == 17 and payload["signal"] is None


@pytest.mark.asyncio
async def test_old_actor_without_account_remains_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    owner = client()
    path = record_failure("old failure", phase="execute", execution=execution(), server=owner)
    payload = json.loads(path.read_text())
    del payload["actor"]["account_id"]
    path.write_text(json.dumps(payload))
    await reconcile_failures(owner)
    assert path.exists()
    owner.tasks.reconcile_attempts.assert_not_awaited()


def test_bootstrap_failure_scope_comes_from_launcher_environment_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://server.example/api/")
    monkeypatch.setenv("SIMSTACK_LOCAL_EXECUTION_TOKEN", "secret-local-capability")
    monkeypatch.setenv("SIMSTACK_EXECUTION_RESOURCE", "wrong-inherited-resource")
    monkeypatch.setenv("SIMSTACK_EXECUTION_ACCOUNT_ID", "test-account")
    identity = execution()
    path = record_failure("API initialize unavailable", phase="bootstrap", execution=identity, resource="cluster")
    payload = json.loads(path.read_text())
    assert payload["actor"]["server_url"] == "https://server.example/api"
    assert payload["actor"]["resource"] == "cluster"
    assert payload["actor"]["authorization"] == "local"
    assert len(payload["actor"]["credential_fingerprint"]) == 64
    assert "secret-local-capability" not in path.read_text()


def test_invalid_bootstrap_url_cannot_mask_original_failure_or_leak_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://user:secret@server.example/api")
    path = record_failure("initialization failed", phase="bootstrap", execution=execution(), resource="cluster")
    payload = json.loads(path.read_text())
    assert payload["actor"] is None and payload["error"] == "initialization failed"
    assert "secret" not in path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 409])
async def test_already_rejected_evidence_is_retained_without_blocking_other_outcomes(tmp_path, monkeypatch, status):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    runner = client()
    rejected_execution = execution()
    rejected = record_failure("rejected original failure", phase="subprocess", execution=rejected_execution,
        server=runner, rejected_status=status)
    valid = record_failure("another process failed", phase="subprocess", execution=execution(), server=runner)
    async def reconcile(*, outcomes, **kwargs):
        assert "rejected_status" not in outcomes[0]
        if outcomes[0]["execution"] == rejected_execution:
            raise RunnerAPIError("still rejected", status_code=status)
        return {"claims": []}
    runner.tasks.reconcile_attempts.side_effect = reconcile
    await reconcile_failures(runner)
    assert runner.tasks.reconcile_attempts.await_count == 2
    assert rejected.is_file() and not valid.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 409])
async def test_first_observed_rejection_does_not_block_later_receipts(tmp_path, monkeypatch, status):
    from simstack.runner_runtime.pending_execution_failures import record_observed_failure, reconcile_observed_failure
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    runner = client()
    pending = record_observed_failure("known worker exit", phase="subprocess", execution=execution(), server=runner, exit_code=17)
    path, outcome = pending
    before = json.loads(path.read_text())
    async def reconcile(*, outcomes, **kwargs):
        if outcomes[0]["execution"] == outcome["execution"]:
            assert kwargs["request_id"] == outcome["request_id"]
            raise RunnerAPIError("still rejected", status_code=status)
        return {"claims": []}
    runner.tasks.reconcile_attempts.side_effect = reconcile
    assert await reconcile_observed_failure(runner, pending) is False
    stored = json.loads(path.read_text())
    assert stored == {**before, "rejected_status": status}
    valid = record_failure("another process failed", phase="subprocess", execution=execution(), server=runner)
    await reconcile_failures(runner)
    assert path.is_file() and not valid.exists()
    assert runner.tasks.reconcile_attempts.await_count == 3
