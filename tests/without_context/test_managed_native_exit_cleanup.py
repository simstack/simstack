"""Windows Job interface and durable exit controls for managed native owners."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.models.document import ObjectId
from simstack.models.parameters import Resource
from simstack.runner_runtime import pending_execution_failures as journal
from simstack.server_api.http_transport import RunnerAPIError


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [17, -9], ids=["exit", "signal"])
@pytest.mark.parametrize("state", ["active", "completed", "failed", "outage"])
async def test_managed_job_exit_preserves_exact_attempt_and_waits_for_cleanup(
    monkeypatch, tmp_path, code, state,
):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    execution = {"task_id": str(ObjectId()), "generation": str(uuid4()),
        "claim_id": str(uuid4()), "start_id": str(uuid4())}
    entry = SimpleNamespace(id=execution["task_id"], job_id=None,
        parameters=SimpleNamespace(queue="default", in_docker=False))
    primary_error = "original scientific error" if state == "failed" else None
    result = SimpleNamespace(status=state if state in {"completed", "failed"} else "running",
        error=primary_error, results_references=["scientific-value"] if state == "completed" else [])
    cleanup_started, allow_cleanup, api_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stopped = False
    offline = state == "outage"
    evidence = []

    async def stop_job():
        nonlocal stopped
        cleanup_started.set()
        await allow_cleanup.wait()
        stopped = True

    job = SimpleNamespace(name="original-job-" + uuid4().hex, stop=AsyncMock(side_effect=stop_job))
    process = SimpleNamespace(pid=54321, returncode=code, wait=AsyncMock(return_value=code),
        _simstack_windows_job=job)
    monkeypatch.setattr("simstack.runner_runtime.native_observations._science_stopped", lambda _: stopped)

    async def read(_):
        api_read.set()
        if offline:
            raise RunnerAPIError("API unavailable", status_code=503)
        return result

    async def fail(original, error, **kwargs):
        assert original is entry and stopped
        result.status, result.error = "failed", error

    async def reconcile(*, claims, outcomes, request_id=None):
        if offline:
            raise RunnerAPIError("API unavailable", status_code=503)
        if claims:
            assert claims == [execution]
            return {"claims": [{"execution": execution,
                "state": "terminal" if result.status in {"completed", "failed"} else "active"}]}
        for outcome in outcomes:
            assert outcome["execution"] == execution
            assert "cleanup" not in outcome and "execution_stopped" not in outcome
            evidence.append(outcome)
        return {"outcomes": []}

    server = SimpleNamespace(requests=SimpleNamespace(server_url="https://example.test/api",
        authorization="runner", resource="test", bootstrap_data={"account_id": "original-account"}),
        tasks=SimpleNamespace(execution=lambda _: dict(execution), get_outcome=AsyncMock(side_effect=read),
            fail_attempt=AsyncMock(side_effect=fail), reconcile_attempts=AsyncMock(side_effect=reconcile),
            termination_confirmed=AsyncMock()))
    service = NodeExecutionService(Resource(value="test"), 1, 1, None)
    observer = asyncio.create_task(service._observe_process(entry, process, server=server, workdir=tmp_path))
    try:
        await asyncio.wait_for(cleanup_started.wait(), 3)
        (path, receipt), = journal.pending_failures()
        assert receipt["execution"] == execution
        assert receipt["cleanup"] == {"scope": journal.cleanup_scope(),
            "process_group": None, "windows_job": job.name}
        assert receipt["exit_code"] == (code if code > 0 else None)
        assert receipt["signal"] == (-code if code < 0 else None)
        await journal.reconcile_failures(server)
        assert not observer.done() and not stopped
        assert path.exists() == (state in {"active", "outage"})
        server.tasks.fail_attempt.assert_not_awaited()
        assert result.error == primary_error

        allow_cleanup.set()
        await asyncio.wait_for(api_read.wait(), 3)
        if offline:
            (_, retained), = journal.pending_failures()
            assert retained["execution"] == execution and "cleanup" not in retained
            assert retained["request_id"] == receipt["request_id"]
            offline = False
        await asyncio.wait_for(observer, 3)
        assert stopped and evidence
        assert {item["request_id"] for item in evidence} == {receipt["request_id"]}
        assert all(item["exit_code"] == receipt["exit_code"] and item["signal"] == receipt["signal"]
            for item in evidence)
        assert not list(journal.pending_failures())
        server.tasks.termination_confirmed.assert_not_awaited()
        if state in {"completed", "failed"}:
            assert result.status == state and result.error == primary_error
            assert result.results_references == (["scientific-value"] if state == "completed" else [])
            server.tasks.fail_attempt.assert_not_awaited()
        else:
            assert result.status == "failed"
            server.tasks.fail_attempt.assert_awaited_once()
    finally:
        allow_cleanup.set()
        if not observer.done():
            observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["journal", "api"])
async def test_managed_job_retains_verified_exit_until_one_durable_sink_recovers(
    monkeypatch, tmp_path, recovery,
):
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path))
    execution = {"task_id": str(ObjectId()), "generation": str(uuid4()),
        "claim_id": str(uuid4()), "start_id": str(uuid4())}
    server = SimpleNamespace(requests=SimpleNamespace(server_url="https://example.test/api",
        authorization="runner", resource="test", bootstrap_data={"account_id": "original-account"}),
        tasks=SimpleNamespace(reconcile_attempts=AsyncMock()))
    entry = SimpleNamespace(id=execution["task_id"], job_id=None,
        parameters=SimpleNamespace(queue="default", in_docker=False))
    job = SimpleNamespace(name="original-job", stop=AsyncMock())
    process = SimpleNamespace(pid=54321, returncode=17, wait=AsyncMock(return_value=17),
        _simstack_windows_job=job)
    pending = journal.record_observed_failure("physical exit", phase="subprocess", execution=execution,
        server=server, exit_code=17, cleanup={"windows_job": job.name, "scope": journal.cleanup_scope()})
    write = journal.record_failure
    attempts = 0

    def unavailable(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if recovery == "api" or attempts < 2:
            raise OSError("journal update unavailable")
        return write(*args, **kwargs)

    monkeypatch.setattr(journal, "record_failure", unavailable)
    server.tasks.reconcile_attempts.side_effect = (
        RunnerAPIError("API unavailable", status_code=503) if recovery == "journal" else None)
    service = NodeExecutionService(Resource(value="test"), 1, 1, None)
    released = await asyncio.wait_for(service._cleanup_native_process(entry, process,
        server=server, workdir=tmp_path, pending=pending), 3)
    assert released[1]["request_id"] == pending[1]["request_id"] and "cleanup" not in released[1]
    assert released[1]["execution"] == execution and released[1]["exit_code"] == 17
    job.stop.assert_awaited_once()
    if recovery == "journal":
        (_, stored), = journal.pending_failures()
        assert "cleanup" not in stored and stored["request_id"] == pending[1]["request_id"]
        assert attempts == 2
    else:
        assert not list(journal.pending_failures())
        server.tasks.reconcile_attempts.assert_awaited_once()
