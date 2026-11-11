"""Recovery respects task deletion and the submitter's original PID scope."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.services import slurm_status_service as watcher
from simstack.runner_runtime import pending_execution_failures as failures
from simstack.runner_runtime import slurm_submission_intents as intents
from simstack.server_api.http_transport import RunnerAPIError


def server():
    return SimpleNamespace(
        requests=SimpleNamespace(
            server_url="https://slurm-intents.invalid/api", authorization="runner",
            resource="local", bootstrap_data={"account_id": "test-account"},
        ),
        tasks=SimpleNamespace(execution=lambda entry: entry.execution, get_task=AsyncMock()),
        slurm=SimpleNamespace(
            scheduler_watchlist=AsyncMock(return_value={"submissions": [{
                "execution": {"task_id": "unrelated"}, "submission_id": "another-submission",
                "job_id": "42",
            }]}),
            scheduler_accepted=AsyncMock(), scheduler_observations=AsyncMock(),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "submitting", "accepted"])
@pytest.mark.parametrize("status_code", [404, 403, 503])
async def test_orphan_intent_does_not_hide_jobs_or_unrelated_api_errors(
    monkeypatch, tmp_path, phase, status_code,
):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = server()
    task_id = "d" * 24
    entry = SimpleNamespace(id=task_id, execution={"task_id": task_id},
        parameters=SimpleNamespace(resource="local"))
    intent = intents.update_intent(api, intents.begin_intent(api, entry),
        phase=phase, **({"job_id": "41"} if phase == "accepted" else {}))
    error = RunnerAPIError("intent endpoint unavailable", status_code=status_code)
    api.tasks.get_task.side_effect = error
    api.slurm.scheduler_accepted.side_effect = error
    monkeypatch.setattr(watcher, "context", SimpleNamespace(server=api))
    monkeypatch.setattr(watcher, "reconcile_failures", AsyncMock())
    monkeypatch.setattr(watcher, "observe_job", lambda job: {
        "job_id": job, "kind": "active", "state": "RUNNING",
    })

    if status_code != 404:
        with pytest.raises(RunnerAPIError) as raised:
            await watcher.SlurmStatusService("local", 1).execute()
        assert raised.value is error
        assert intents.read_intent(api, entry.id) == intent
        api.slurm.scheduler_observations.assert_not_awaited()
    else:
        await watcher.SlurmStatusService("local", 1).execute()
        assert intents.read_intent(api, entry.id) == (None if phase == "prepared" else intent)
        observation, = api.slurm.scheduler_observations.await_args.args[0]
        assert observation["job_id"] == "42" and observation["state"] == "RUNNING"


@pytest.mark.parametrize("original,current", [
    (None, {"hostname": "local"}),
    ({"hostname": "local"}, None),
    ({"hostname": "other"}, {"hostname": "local"}),
    ({"hostname": "local", "pid_namespace": "old"},
     {"hostname": "local", "pid_namespace": "new"}),
])
def test_unknown_or_foreign_submitter_scope_never_queries_local_pid(monkeypatch, original, current):
    monkeypatch.setattr(failures, "cleanup_scope", lambda: current)
    def local_query(*args):
        pytest.fail("A local PID cannot prove a foreign submitter stopped")
    monkeypatch.setattr(intents.os, "kill", local_query)
    monkeypatch.setattr(intents, "process_identity", local_query)
    assert intents.submitter_alive({"process_id": 42, "process_birth": "birth", "scope": original}) is None


def test_same_scope_dead_submitter_still_proves_stopped(monkeypatch):
    scope = {"hostname": "local"}
    monkeypatch.setattr(failures, "cleanup_scope", lambda: scope)
    def absent(*args):
        raise ProcessLookupError(42)
    monkeypatch.setattr(intents.os, "kill", absent)
    assert intents.submitter_alive({"process_id": 42, "scope": scope}) is False


def test_process_rebinding_records_its_scope_but_acceptance_preserves_it(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = server()
    task_id = "a" * 24
    entry = SimpleNamespace(id=task_id, execution={"task_id": task_id},
        parameters=SimpleNamespace(resource="local"))
    value = intents.begin_intent(api, entry)
    first = {"hostname": "first"}
    second = {"hostname": "second"}
    monkeypatch.setattr(failures, "cleanup_scope", lambda: first)
    value = intents.update_intent(api, value, phase="submitting", process_id=42)
    monkeypatch.setattr(failures, "cleanup_scope", lambda: second)
    value = intents.update_intent(api, value, phase="accepted", job_id="41")
    assert value["scope"] == first
    value = intents.update_intent(api, value, process_id=43)
    assert intents.read_intent(api, entry.id)["scope"] == second
