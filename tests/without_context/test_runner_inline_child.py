from __future__ import annotations

import asyncio
import os
import signal
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.runner_api import ClaimedTask
from simstack.core.runner_execution.inline_child import execute_inline_child

from simstack.runner_runtime.processes import (
    read_worker_stream, signal_worker_tree, supervise_worker,
    terminate_worker_tree, wait_for_worker_exit,
)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix process-group lifecycle")
async def test_exited_worker_descendant_pipes_do_not_hang_and_group_is_terminated():
    script = """
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
print(child.pid, flush=True)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    drains = []
    try:
        descendant_pid = int(await asyncio.wait_for(process.stdout.readline(), timeout=2))
        drains = [asyncio.create_task(read_worker_stream(process.stdout)), asyncio.create_task(read_worker_stream(process.stderr))]
        assert await asyncio.wait_for(wait_for_worker_exit(process), timeout=2) == 0
        await asyncio.wait_for(terminate_worker_tree(process), timeout=2)
        await asyncio.wait_for(asyncio.gather(*drains), timeout=2)
        for _ in range(20):
            try:
                os.kill(descendant_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("Descendant survived claim-bound worker cleanup")
    finally:
        signal_worker_tree(process, signal.SIGKILL)
        for drain in drains:
            drain.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix process-group lifecycle")
async def test_supervisor_finishes_when_exited_worker_descendant_keeps_pipes_open():
    script = """
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
print(child.pid, flush=True)
print('worker completed', flush=True)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        result = await asyncio.wait_for(supervise_worker(process), timeout=2)
        assert result.returncode == 0
        assert "worker completed" in result.stdout
        descendant_pid = int(result.stdout.splitlines()[0])
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
    finally:
        signal_worker_tree(process, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix process-group lifecycle")
async def test_shared_worker_supervisor_renews_lease_while_child_runs():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(0.2)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    renewals = []

    async def renew():
        renewals.append(True)

    try:
        result = await supervise_worker(process, renew_lease=renew, renew_every=0.05)
        assert result.returncode == 0
        assert renewals
    finally:
        signal_worker_tree(process, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix process-group lifecycle")
async def test_shared_worker_supervisor_cancellation_terminates_child():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    task = asyncio.create_task(supervise_worker(process))
    try:
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert process.returncode is not None
    finally:
        signal_worker_tree(process, signal.SIGKILL)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["spawn", "bootstrap_exit"])
async def test_inline_worker_failure_is_reported_with_child_only_token(monkeypatch, tmp_path, failure):
    claim = ClaimedTask(
        task={"id": "0123456789abcdef01234567"}, claim_id="child-claim", attempt=2,
        lease_until="2026-10-09T20:00:00Z", execution_token="child-token",
        execution_token_expires_at="2026-10-09T20:00:00Z",
    )
    context = SimpleNamespace(
        config=SimpleNamespace(resource="resource", project_root=tmp_path, workdir=tmp_path),
        runner_api=SimpleNamespace(server_url="http://example.test/api", runner_token="parent-token"),
    )
    monkeypatch.setattr("simstack.core.runner_execution.inline_child.context", context)
    failure_client = SimpleNamespace(fail_task=AsyncMock())
    client_arguments = []

    def client(**kwargs):
        client_arguments.append(kwargs)
        return failure_client

    monkeypatch.setattr("simstack.core.runner_execution.inline_child.ExecutionRunnerClient", client)
    if failure == "spawn":
        monkeypatch.setattr("simstack.core.runner_execution.inline_child.asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError("cannot spawn")))
    else:
        monkeypatch.setattr("simstack.core.runner_execution.inline_child.asyncio.create_subprocess_exec", AsyncMock(return_value=object()))
        monkeypatch.setattr("simstack.core.runner_execution.inline_child.supervise_worker", AsyncMock(return_value=SimpleNamespace(returncode=1)))
    with pytest.raises(Exception):
        await execute_inline_child(claim)
    assert client_arguments == [{"server_url": "http://example.test/api", "runner_token": "child-token"}]
    failure_client.fail_task.assert_awaited_once()
    args, kwargs = failure_client.fail_task.call_args
    assert args == (claim.task["id"],) and kwargs["claim_id"] == claim.claim_id and kwargs["attempt"] == claim.attempt
