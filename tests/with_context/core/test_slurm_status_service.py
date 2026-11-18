from datetime import datetime, timezone
from uuid import uuid4
import pytest

from simstack.core.context import context
from simstack.core.services.slurm_status_service import SlurmStatusService, resource_uses_slurm_queue
from simstack.models import NodeRegistry, Parameters
from simstack.models.parameters import SlurmParameters

async def submitted(server_storage):
    task = NodeRegistry(name="fixture", status="submitted", function_hash="", arg_hash="input",
        func_mapping="simstack.methods.trivial.echo", parameters=Parameters(resource="test", queue="slurm-queue",
        slurm_parameters=SlurmParameters(tasks_per_node=None)))
    await server_storage.save(task)
    from simstack.runner_runtime.launch_claims import acquire_work
    entry = (await acquire_work(context.server))[0]
    object.__setattr__(entry, "_execution_start_id", str(uuid4()))
    submission = await context.server.slurm.scheduler_submission(entry, str(uuid4()))
    await context.server.slurm.scheduler_accepted(context.server.tasks.execution(entry), submission["submission_id"], "42")
    return entry

@pytest.mark.asyncio
async def test_slurm_enabled_only_for_the_authenticated_resource(domain_bridge):
    domain_bridge.auth.resource.queue = "slurm-queue"
    assert await resource_uses_slurm_queue(domain_bridge.auth.resource.resource_str)

@pytest.mark.asyncio
@pytest.mark.parametrize("kind,state,expected", [
    ("unknown", None, "slurm_queued"),
    ("active", "RUNNING", "slurm_queued"),
    ("terminal", "FAILED", "failed"),
    ("terminal", "TIMEOUT", "timeout"),
    ("terminal", "OUT_OF_MEMORY", "failed"),
    ("terminal", "NODE_FAIL", "failed"),
])
async def test_scheduler_evidence_reconciles_queued_tasks(server_storage, monkeypatch, kind, state, expected):
    entry = await submitted(server_storage)
    observation = {"job_id": "42", "kind": kind, "state": state,
        "observed_at": datetime.now(timezone.utc).isoformat(), "source": "test",
        "exit_code": 17, "signal": 0, "reason": "scheduler evidence"}
    monkeypatch.setattr("simstack.core.services.slurm_status_service.observe_job", lambda job_id: observation)
    await SlurmStatusService("test", 60).execute()
    stored = await context.server.tasks.get_task(entry.id)
    assert stored.status == expected
    assert stored.job_id == "42"

@pytest.mark.asyncio
async def test_bootstrap_failure_is_delivered_from_local_journal(server_storage):
    from simstack.runner_runtime.pending_execution_failures import record_failure, pending_failures
    entry = await submitted(server_storage)
    path = record_failure("API bootstrap unavailable", phase="bootstrap", execution=context.server.tasks.execution(entry), exit_code=1, server=context.server, resource="test")
    assert path.is_file()
    await SlurmStatusService("test", 60).execute()
    stored = await context.server.tasks.get_task(entry.id)
    assert stored.status == "failed"
    assert stored.error == "API bootstrap unavailable"
    assert list(pending_failures()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_error", [False, True])
async def test_sweep_reads_queue_once_and_checks_accounting_per_missing_job(monkeypatch, queue_error):
    from unittest.mock import AsyncMock
    from simstack.util import slurm_observation

    jobs = ["42", "42_1", "42_2", "43", "44"]
    submissions = [{"job_id": job, "execution": {"task_id": job}, "submission_id": str(uuid4())}
                   for job in jobs]
    monkeypatch.setattr(context.server.slurm, "scheduler_watchlist", AsyncMock(return_value={"submissions": submissions}))
    record = AsyncMock()
    monkeypatch.setattr(context.server.slurm, "scheduler_observations", record)
    calls = []
    def query(command):
        calls.append(command)
        if command[0] == "squeue":
            assert "--array" in command
            if queue_error:
                raise RuntimeError("queue unavailable")
            return "42_1|RUNNING|R|active|user|1:00|node01|None"
        job = command[command.index("--jobs") + 1]
        return "43|COMPLETED|0:0|2026-11-09T12:00:00|None" if job == "43" else ""
    monkeypatch.setattr(slurm_observation, "_run", query)
    await SlurmStatusService("test", 60).execute()
    assert [command[0] for command in calls].count("squeue") == 1
    assert [command[command.index("--jobs") + 1] for command in calls if command[0] == "sacct"] == (
        jobs if queue_error else ["42_2", "43", "44"])
    facts = {fact["job_id"]: fact for fact in record.await_args.args[0]}
    assert facts["43"]["kind"] == "terminal"
    assert facts["44"]["kind"] == "unknown"
    assert facts["44"]["job_absent"] is (not queue_error)
    assert facts["42_2"]["job_absent"] is (not queue_error)
    for job in jobs[:2]:
        assert facts[job]["kind"] == ("unknown" if queue_error else "active")
