"""Execution dispatch through the actual Server domain HTTP API."""
from simstack.runner_runtime.launch_claims import acquire_work
import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from simstack.core.context import context
from simstack.core.node import Node, node
from simstack.core.definitions import TaskStatus
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.models import NodeRegistry, FloatData
from simstack.models.parameters import Parameters, Queue, Resource
from simstack.runner_runtime.launch_claims import task_execution, current_execution
from simstack.server_api.http_transport import RunnerAPIError


_NATIVE_PROCESS_LAUNCH = Node.run_node_as_process

@pytest.fixture
def resource():
    return Resource(value="test")


@pytest.fixture
def node_execution_service(resource):
    return NodeExecutionService(resource, interval=1, max_concurrent=2,
        shutdown_event=asyncio.Event(), detach=False)


async def submitted(server_storage, *, parameters=None, status=TaskStatus.SUBMITTED):
    entry = NodeRegistry(name="test_node", status=status,
        parameters=parameters or Parameters(resource="test"),
        func_mapping="tests.execution.probe", function_hash="", arg_hash="probe")
    await server_storage.save(entry)
    generation = str(uuid4())
    await server_storage.get_collection(NodeRegistry).update_one({"_id": entry.id},
        {"$set": {"_runner_generation": generation}})
    object.__setattr__(entry, "_execution_generation", generation)
    return entry


async def acquired(server_storage, *, parameters=None):
    original = await submitted(server_storage, parameters=parameters)
    tasks = await acquire_work(context.server, capacity=1)
    assert len(tasks) == 1 and tasks[0].id == original.id
    entry = tasks[0]
    with task_execution(entry):
        assert current_execution()["start_id"]
    return entry


@pytest.mark.asyncio
async def test_node_execution_service_execute_no_tasks(node_execution_service, domain_bridge):
    await node_execution_service.execute()
    assert not node_execution_service._running_tasks
    assert any(path.endswith("/work/acquire") for _, path, _ in domain_bridge.api.requests.transport.calls)


@pytest.mark.asyncio
async def test_node_execution_service_execute_with_tasks(server_storage, node_execution_service):
    entry = await submitted(server_storage)
    with patch.object(node_execution_service, "run_node", AsyncMock(return_value=True)) as run:
        await node_execution_service.execute()
        await asyncio.gather(*node_execution_service._running_tasks)
    run.assert_awaited_once()
    assert run.await_args.args[0].id == entry.id
    assert (await context.server.tasks.get_task(entry.id)).status == TaskStatus.RETRIEVED


@pytest.mark.asyncio
async def test_execute_terminates_inflight_tasks(server_storage, node_execution_service):
    entry = await submitted(server_storage, status=TaskStatus.TERMINATING)
    with patch("simstack.core.services.node_execution_service.terminate_task", AsyncMock()) as terminate:
        await node_execution_service.execute()
    terminate.assert_awaited_once()
    assert terminate.await_args.args[0].id == entry.id
    assert terminate.await_args.kwargs == {"runner_pid": node_execution_service._pid}


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 17])
async def test_node_execution_service_run_node_default_queue(server_storage, node_execution_service, returncode,
                                                            monkeypatch, tmp_path):
    from simstack.runner_runtime.pending_execution_failures import pending_failures
    monkeypatch.setenv("SIMSTACK_PENDING_FAILURE_DIR", str(tmp_path / "exit-evidence"))
    entry = await acquired(server_storage)
    read_outcome = context.server.tasks.get_outcome
    async def read_after_exit(*args, **kwargs):
        records = list(pending_failures())
        assert [record["exit_code"] for _, record in records] == ([17] if returncode else [])
        return await read_outcome(*args, **kwargs)
    monkeypatch.setattr(context.server.tasks, "get_outcome", read_after_exit)
    async def spawn(*args, **kwargs):
        await context.server.tasks.start_attempt(entry, process_id=99999)
        await context.server.tasks.complete_attempt(entry, return_kind="bool")
        return SimpleNamespace(pid=99999, returncode=returncode, wait=AsyncMock(return_value=returncode))
    with patch("simstack.core.services.node_execution_service._spawn_native_process", side_effect=spawn) as launch, \
         patch("simstack.core.services.node_execution_service.run_node_from_registry", AsyncMock(side_effect=AssertionError("in-process managed science"))):
        assert await node_execution_service.run_node(entry)
    launch.assert_awaited_once()
    row = await server_storage.core_engine.get_collection("node_registry").find_one({"_id": entry.id})
    history = next(iter(row["_runner_attempt_outcomes"].values()))
    assert row["status"] == "completed" and not row.get("error")
    assert [item["exit_code"] for item in history.get("evidence", [])] == ([17] if returncode else [])


@pytest.mark.asyncio
async def test_foreground_completed_result_returns_when_both_evidence_sinks_are_unavailable(
    server_storage, node_execution_service, monkeypatch,
):
    from simstack.runner_runtime import pending_execution_failures
    entry = await acquired(server_storage)
    async def spawn(*args, **kwargs):
        await context.server.tasks.start_attempt(entry, process_id=99999)
        await context.server.tasks.complete_attempt(entry, return_kind="bool")
        return SimpleNamespace(pid=99999, returncode=17, wait=AsyncMock(return_value=17))
    def journal_unavailable(*args, **kwargs):
        raise PermissionError("journal unavailable")
    monkeypatch.setattr(pending_execution_failures, "record_failure", journal_unavailable)
    monkeypatch.setattr(context.server.tasks, "reconcile_attempts", AsyncMock(
        side_effect=RunnerAPIError("receipt unavailable", status_code=503)))
    monkeypatch.setattr("simstack.core.services.node_execution_service._spawn_native_process", spawn)
    assert await asyncio.wait_for(node_execution_service.run_node(entry), 5)
    assert (await context.server.tasks.get_task(entry.id)).status == TaskStatus.COMPLETED
    context.server.tasks.reconcile_attempts.assert_awaited_once()


@pytest.mark.asyncio
async def test_node_execution_service_run_node_default_docker(server_storage, node_execution_service):
    entry = await acquired(server_storage, parameters=Parameters(resource="test", in_docker=True))
    with patch("simstack.core.services.node_execution_service.run_docker", AsyncMock(return_value=True)) as docker, \
         patch("simstack.core.services.node_execution_service.run_node_from_registry", AsyncMock(side_effect=AssertionError("host dispatch"))):
        assert await node_execution_service.run_node(entry)
    docker.assert_awaited_once()
    assert docker.await_args.args == (entry,)
    assert callable(docker.await_args.kwargs["on_observed_process"])


@pytest.mark.asyncio
async def test_self_docker_handoff_is_visible_to_current_resource_runner(server_storage):
    entry = await acquired(server_storage)
    await context.server.tasks.handoff(entry, resource="test", in_docker=True)
    saved = await context.server.tasks.get_task(entry.id)
    assert saved.parameters.resource == "test"
    assert saved.parameters.in_docker
    assert saved.status == TaskStatus.SUBMITTED
    reclaimed = await acquire_work(context.server, capacity=1)
    assert [item.id for item in reclaimed] == [entry.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("in_docker", [False, True])
async def test_node_execution_service_run_node_slurm_queue(server_storage, node_execution_service, in_docker):
    entry = await acquired(server_storage, parameters=Parameters(resource="test", queue=Queue.SLURM_QUEUE, in_docker=in_docker))
    with patch("simstack.core.services.node_execution_service.submit_node", AsyncMock(return_value=True)) as submit:
        assert await node_execution_service.run_node(entry)
    submit.assert_awaited_once_with(entry)
    assert entry.parameters.in_docker is in_docker


@pytest.mark.asyncio
async def test_detached_spawn_failure_is_sanitized_and_marks_task_failed(server_storage, resource, monkeypatch, caplog):
    secret = "mongodb://runner-user:runner-password@db.internal:27017/simstack"
    entry = await acquired(server_storage)
    service = NodeExecutionService(resource, 1, 1, None, detach=True)
    monkeypatch.setattr(service, "write_node_event", AsyncMock())
    spawn = AsyncMock(side_effect=OSError(f"spawn failed at {secret}"))
    monkeypatch.setattr("simstack.core.services.node_execution_service._spawn_native_process", spawn)
    assert not await service.run_node(entry)
    saved = await context.server.tasks.get_task(entry.id)
    assert saved.status == TaskStatus.FAILED
    assert secret not in saved.error
    assert "runner-user" not in caplog.text and "runner-password" not in caplog.text
    assert not any(key.startswith("SIMSTACK_DB_") for key in spawn.await_args.kwargs["env"])


@pytest.mark.asyncio
async def test_windows_run_node_spawn_has_no_console(server_storage, resource, monkeypatch):
    entry = await acquired(server_storage)
    service = NodeExecutionService(resource, 1, 1, None, detach=True)
    monkeypatch.setattr(service, "write_node_event", AsyncMock())
    monkeypatch.setattr("simstack.core.services.node_execution_service.platform.system", lambda: "Windows")
    async def spawn_child(*args, **kwargs):
        await context.server.tasks.start_attempt(entry, process_id=4321)
        return SimpleNamespace(pid=4321, returncode=None, wait=asyncio.Event().wait)
    spawn = AsyncMock(side_effect=spawn_child)
    monkeypatch.setattr("simstack.core.services.node_execution_service._spawn_native_process", spawn)
    assert await service.run_node(entry)
    command = spawn.await_args.args
    import sys
    assert command[:3] == (sys.executable, "-m", "simstack.core.run_node")
    flags = spawn.await_args.kwargs["creationflags"]
    assert flags & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert not flags & getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    for observer in service._observers:
        observer.cancel()
    await asyncio.gather(*service._observers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unstarted", "stored-error", "cancelling", "competing-start", "rerun"])
async def test_pinned_foreground_child_exit_before_journal_is_fenced(server_storage, resource, monkeypatch, state):
    import sys
    from simstack.runner_runtime import execution_environment

    entry = await acquired(server_storage)
    original = context.server.tasks.execution(entry)
    entry.repository_revision = "prepared-test-revision"
    collection = server_storage.core_engine.get_collection("node_registry")
    changes = {
        "unstarted": {},
        "stored-error": {"error": "Scientific child error already recorded"},
        "cancelling": {"status": "terminating", "error": "User cancelled"},
        "competing-start": {"status": "running", "_runner_start_id": "another-process"},
        "rerun": {"status": "submitted", "_runner_generation": "another-generation"},
    }[state]
    if changes:
        await collection.update_one({"_id": entry.id}, {"$set": changes})
    monkeypatch.setattr(execution_environment, "prepare_task", AsyncMock())
    monkeypatch.setattr(execution_environment, "task_launch_command",
        lambda *args: [sys.executable, "-c", "raise SystemExit(17)"])
    service = NodeExecutionService(resource, 1, 1, None, detach=False)
    monkeypatch.setattr(service, "write_node_event", AsyncMock())

    assert not await service.run_node(entry)
    current = await context.server.tasks.get_task(entry.id)
    assert context.server.tasks.execution(entry) == original
    if state in {"unstarted", "stored-error"}:
        assert current.status == TaskStatus.FAILED
        assert current.error == (changes.get("error") or
            "run_node child exited without completing the task (code 17)")
        outcome = await context.server.tasks.get_outcome(entry)
        assert outcome.status == TaskStatus.FAILED and outcome.error == current.error
        stored = await context.server.requests.read(f"/tasks/{entry.id}/outcome",
            **{key: value for key, value in original.items() if key != "task_id"})
        assert stored["outcome"]["exit_code"] == 17
        assert stored["outcome"]["signal"] is None
    else:
        row = await collection.find_one({"_id": entry.id})
        assert all(row[key] == value for key, value in changes.items())


@pytest.mark.asyncio
async def test_pinned_foreground_failure_keeps_its_cause_for_reconciliation(server_storage, resource, monkeypatch, tmp_path):
    import json
    import sys
    from simstack.runner_runtime import execution_environment

    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    entry = await acquired(server_storage)
    execution = context.server.tasks.execution(entry)
    entry.repository_revision = "prepared-test-revision"
    monkeypatch.setattr(execution_environment, "prepare_task", AsyncMock())
    monkeypatch.setattr(execution_environment, "task_launch_command",
        lambda *args: [sys.executable, "-c", "raise SystemExit(17)"])
    monkeypatch.setattr(context.server.tasks, "fail_attempt", AsyncMock(
        side_effect=RunnerAPIError("Failure acknowledgement unavailable", status_code=503)))
    monkeypatch.setattr(context.server.tasks, "reconcile_attempts", AsyncMock(
        side_effect=RunnerAPIError("Reconciliation unavailable", status_code=503)))
    service = NodeExecutionService(resource, 1, 1, None, detach=False)
    monkeypatch.setattr(service, "write_node_event", AsyncMock())

    assert not await service.run_node(entry)
    context.server.tasks.fail_attempt.assert_awaited_once()
    records = list((tmp_path / ".pending-execution-failures").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["execution"] == execution
    assert record["phase"] == "subprocess"
    assert record["exit_code"] == 17 and record["signal"] is None
    assert record["error"] == "run_node child exited without completing the task (code 17)"
    assert record["actor"]["resource"] == str(resource)


@pytest.mark.asyncio
async def test_direct_system_exit_marks_task_failed(server_storage, node_execution_service):
    entry = await acquired(server_storage)
    with patch("simstack.core.services.node_execution_service._spawn_native_process", AsyncMock(side_effect=SystemExit(0))):
        assert not await node_execution_service.run_node(entry)
    saved = await context.server.tasks.get_task(entry.id)
    assert saved.status == TaskStatus.FAILED
    assert saved.return_kind == "exception" and saved.error == "0"


@pytest.mark.asyncio
async def test_nested_execution_handoff_does_not_requeue_claimed_task(server_storage):
    entry = await acquired(server_storage)
    stale = entry.model_copy(deep=True)
    object.__setattr__(stale, "_execution_claim_id", "competing-old-claim")
    with pytest.raises(RunnerAPIError, match="HTTP 409"):
        await context.server.tasks.handoff(stale, resource="test", in_docker=True)
    current = await context.server.tasks.get_task(entry.id)
    assert current.status == TaskStatus.RETRIEVED
    assert not current.parameters.in_docker



@node
def admitted_execution_probe(**kwargs):
    return FloatData(value=42.0)


@pytest.mark.asyncio
async def test_admitted_running_attempt_executes_and_commits_through_http(server_storage):
    from simstack.core.services.node_execution_service import run_node_from_registry_with_outcome
    descriptor = context.node_mappings.get_by_name("admitted_execution_probe")
    entry = NodeRegistry(name=descriptor.name, status=TaskStatus.SUBMITTED,
        func_mapping=descriptor.function_mapping, function_hash="", arg_hash="empty-inputs",
        parameters=Parameters(resource="test"))
    await server_storage.save(entry)
    claimed = (await acquire_work(context.server, capacity=1))[0]
    outcome = await run_node_from_registry_with_outcome(claimed)
    assert outcome.success and outcome.return_kind == "model"
    completed = await context.server.tasks.get_task(entry.id)
    assert completed.status == TaskStatus.COMPLETED
    assert len(completed.results_references) == 1
    ref = completed.results_references[0]
    assert (await context.server.values.get_value(ref.variable_mapping, ref.reference)).value == 42.0


@pytest.mark.asyncio
async def test_lost_process_report_does_not_fail_an_admitted_native_child(server_storage, resource, monkeypatch):
    entry = await acquired(server_storage)
    service = NodeExecutionService(resource, 1, 1, None, detach=True)
    monkeypatch.setattr(service, "write_node_event", AsyncMock())
    async def spawn_child(*args, **kwargs):
        await context.server.tasks.start_attempt(entry, process_id=4321)
        return SimpleNamespace(pid=4321, returncode=None, wait=asyncio.Event().wait)
    monkeypatch.setattr("simstack.core.services.node_execution_service._spawn_native_process", spawn_child)
    monkeypatch.setattr(context.server.tasks, "record_process", AsyncMock(side_effect=OSError("report reply lost")))
    assert await service.run_node(entry)
    canonical = await context.server.tasks.get_task(entry.id)
    assert canonical.status == TaskStatus.RUNNING
    assert canonical.error is None
    for observer in service._observers:
        observer.cancel()
    await asyncio.gather(*service._observers, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconstruction_failure_is_reported_once(server_storage, domain_bridge):
    from simstack.core.services.node_execution_service import run_node_from_registry_with_outcome
    entry = await acquired(server_storage)
    outcome = await run_node_from_registry_with_outcome(entry)
    assert not outcome.success and outcome.return_kind == "exception"
    assert (await context.server.tasks.get_task(entry.id)).status == TaskStatus.FAILED
    failures = [path for method, path, body in domain_bridge.api.requests.transport.calls
        if method == "POST" and path.endswith("/attempts/fail")]
    assert len(failures) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode,protocol", [(17, True), (0, False)])
async def test_native_parent_preserves_committed_result_after_launcher_failure(
    server_storage, domain_bridge, monkeypatch, returncode, protocol,
):
    import sys
    from simstack.models import NamedDataReference
    from simstack.core.run_node_protocol import RunNodeResult, encode_run_node_result
    entry = await acquired(server_storage)
    scientific_result = await context.server.values.publish_value(FloatData(value=42.0))
    references = [NamedDataReference(variable_name="result", variable_mapping="simstack.models.base_types.FloatData",
        reference=scientific_result.id)]
    spawn = asyncio.create_subprocess_exec
    async def child_process(*args, **kwargs):
        output = encode_run_node_result(RunNodeResult(True, "model")) if protocol else "cleanup diagnostics"
        process = await spawn(sys.executable, "-c", f"import sys; print({output!r}); sys.exit({returncode})", **kwargs)
        await context.server.tasks.start_attempt(entry, process_id=process.pid)
        with task_execution(entry):
            _, publication = await context.server.values.publish_task_outputs([scientific_result])
        object.__setattr__(entry, "_output_publication", publication)
        await context.server.tasks.complete_attempt(entry, outputs=references, return_kind="model")
        return process
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", child_process)
    invocation = Node.__new__(Node)
    invocation.name, invocation.registry_entry, invocation.parameters = entry.name, entry, entry.parameters
    result = await _NATIVE_PROCESS_LAUNCH(invocation)
    assert result.value == 42.0
    canonical = await context.server.tasks.get_task(entry.id)
    assert canonical.status == TaskStatus.COMPLETED and canonical.error is None
    assert not any(path.endswith("/attempts/fail") for method, path, body in domain_bridge.api.requests.transport.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_poll", [False, True])
async def test_inline_slurm_child_is_stopped_by_its_actual_parent_process(server_storage, monkeypatch, lost_poll):
    import os
    import sys
    from uuid import uuid4
    from simstack.models.document import ObjectId
    from simstack.core.services import task_termination
    parent = await acquired(server_storage, parameters=Parameters(resource="test", queue="slurm-queue"))
    await context.server.tasks.start_attempt(parent)
    descriptor = context.node_mappings.get_by_name("admitted_execution_probe")
    response = await context.server.tasks.create_child({
        "requested_task_id": str(ObjectId()), "invocation_id": str(uuid4()),
        "parent_execution": context.server.tasks.execution(parent), "name": descriptor.name,
        "function_mapping": descriptor.function_mapping, "is_async": False,
        "input_references": [], "arg_hash": "cancel-inline", "call_path": ".parent.inline",
        "parameters": Parameters(resource="self", queue="slurm-queue").model_dump(mode="json", exclude_unset=True),
        "inline": True,
    }, request_id=str(uuid4()))
    child = response["task"]
    assert response["disposition"] == "launch_inline" and child.job_id is None
    assert child._execution_launch_kind == "inline"
    spawn = asyncio.create_subprocess_exec
    processes, cancellations = [], []
    read_outcome = context.server.tasks.get_outcome
    failed_polls = []
    async def outcome_after_one_network_outage(entry):
        if lost_poll and not failed_polls:
            assert processes[0].returncode is None
            failed_polls.append(True)
            raise RunnerAPIError("cancellation poll temporarily unavailable", status_code=503)
        return await read_outcome(entry)
    monkeypatch.setattr(context.server.tasks, "get_outcome", outcome_after_one_network_outage)
    async def cancel_from_host():
        await asyncio.sleep(0.05)
        cancelled = await context.server.tasks.cancel_child(parent.id, context.server.tasks.execution(parent), child, "cancel inline child")
        with pytest.raises(task_termination.TaskTerminationError, match="launching process"):
            await task_termination.terminate_task(cancelled, runner_pid=os.getpid())
        assert (await context.server.tasks.get_task(child.id)).status == TaskStatus.TERMINATING
    async def child_process(*args, **kwargs):
        process = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        await context.server.tasks.start_attempt(child, process_id=process.pid)
        cancellations.append(asyncio.create_task(cancel_from_host()))
        return process
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", child_process)
    monkeypatch.setattr(task_termination, "_run_command", AsyncMock(side_effect=AssertionError("shared allocation was cancelled")))
    invocation = Node.__new__(Node)
    invocation.name, invocation.registry_entry, invocation.parameters = child.name, child, child.parameters
    try:
        with pytest.raises(RuntimeError, match="cancel inline child"):
            await asyncio.wait_for(_NATIVE_PROCESS_LAUNCH(invocation), timeout=5)
        await asyncio.gather(*cancellations)
        assert processes[0].returncode is not None
        assert bool(failed_polls) is lost_poll
        canonical = await context.server.tasks.get_task(child.id)
        assert canonical.status == TaskStatus.FAILED and canonical.error == "cancel inline child"
        assert (await context.server.tasks.get_task(parent.id)).status == TaskStatus.RUNNING
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
async def test_taskgroup_cancellation_uses_the_published_parent_execution(server_storage, domain_bridge, monkeypatch):
    import sys
    from simstack.core.services import task_termination
    parent = await acquired(server_storage, parameters=Parameters(resource="test", queue="slurm-queue"))
    await context.server.tasks.start_attempt(parent)
    invocation = Node(func=admitted_execution_probe._inner, is_async=False, parent_id=parent.id,
        parameters=Parameters(resource="self", queue="slurm-queue"))
    with task_execution(parent):
        await invocation.get_node_registry()
    original_parent = dict(invocation._parent_execution)
    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []
    async def child_process(*args, **kwargs):
        process = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        await context.server.tasks.start_attempt(invocation.registry_entry, process_id=process.pid)
        started.set()
        return process
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", child_process)
    monkeypatch.setattr(task_termination, "_run_command", AsyncMock(side_effect=AssertionError("shared allocation was cancelled")))
    try:
        async with asyncio.TaskGroup() as group:
            task = group.create_task(_NATIVE_PROCESS_LAUNCH(invocation))
            await started.wait()
            await asyncio.sleep(0.05)
            task.cancel()
        assert processes[0].returncode is not None
        canonical = await context.server.tasks.get_task(invocation.id)
        assert canonical.status == TaskStatus.FAILED
        assert canonical.error == f"Terminated because parent task {parent.id} failed"
        cancellations = [body for method, path, body in domain_bridge.api.requests.transport.calls if path.endswith("/children/cancel")]
        assert len(cancellations) == 1
        assert cancellations[0]["parent_execution"] == original_parent
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        reservation = getattr(invocation, "_inline_launch_reservation", None)
        if reservation is not None:
            reservation.close()


@pytest.mark.asyncio
async def test_user_sdk_cancelling_root_wait_never_kills_a_runner_pid(server_storage, domain_bridge, monkeypatch):
    from simstack.core.services import task_termination
    root = await submitted(server_storage, status=TaskStatus.RUNNING)
    root.process_id = 987654
    await server_storage.save(root)
    monkeypatch.setattr(context, "_server", domain_bridge.user_api)
    monkeypatch.setattr(task_termination, "_kill_process_tree", lambda pid: pytest.fail("User SDK killed a remote Runner PID locally"))
    invocation = Node.__new__(Node)
    invocation.name, invocation.registry_entry, invocation.parameters = root.name, root, root.parameters
    invocation.parent_id, invocation._parent_execution = None, None
    waiter = asyncio.create_task(invocation._wait_for_remote_completion())
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await domain_bridge.runner_api.tasks.get_task(root.id)).status == TaskStatus.RUNNING
    assert not any(method == "POST" for method, path, body in domain_bridge.user_api.requests.transport.calls)


@pytest.mark.asyncio
async def test_lost_start_reply_reconciles_committed_command(server_storage, monkeypatch):
    entry = await acquired(server_storage)
    command = context.server.requests.command
    calls = []
    async def lost_reply(path, body, **kwargs):
        result = await command(path, body, **kwargs)
        if path == "/attempts/start":
            calls.append(kwargs["request_id"])
            if len(calls) == 1:
                raise RunnerAPIError("committed response lost", status_code=None)
        return result
    monkeypatch.setattr(context.server.requests, "command", lost_reply)
    started = await asyncio.wait_for(context.server.tasks.start_attempt(entry, process_id=321), 2)
    assert started.status == TaskStatus.RUNNING
    assert started._execution_start_id == entry._execution_start_id
    assert calls == [entry._execution_start_id]


@pytest.mark.asyncio
async def test_start_poll_recovers_api_without_waiting_for_scientific_exit(server_storage, resource, monkeypatch):
    import sys
    entry = await acquired(server_storage)
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    service = NodeExecutionService(resource, 1, 1, None)
    try:
        await context.server.tasks.start_attempt(entry, process_id=process.pid)
        read = context.server.tasks.get_task
        calls = 0
        async def recovered(task_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RunnerAPIError("temporary read outage", status_code=503)
            return await read(task_id)
        monkeypatch.setattr(context.server.tasks, "get_task", recovered)
        await asyncio.wait_for(service._wait_for_start(entry, process), 2)
        assert calls == 2 and process.returncode is None
    finally:
        process.kill()
        await process.wait()


@pytest.mark.asyncio
async def test_managed_docker_parent_releases_capacity_before_queued_child(server_storage, resource, monkeypatch):
    parent = await acquired(server_storage, parameters=Parameters(resource="test", in_docker=True))
    child = await acquired(server_storage, parameters=Parameters(resource="test", in_docker=True))
    service = NodeExecutionService(resource, 1, 1, None)
    service.write_node_event = AsyncMock()
    child_started = asyncio.Event()
    async def docker(entry, *, on_observed_process=None, retry_unpersisted_evidence=False):
        await context.server.tasks.start_attempt(entry)
        if entry.id == parent.id:
            await child_started.wait()
        else:
            child_started.set()
        await context.server.tasks.complete_attempt(entry, return_kind="bool")
        return True
    monkeypatch.setattr("simstack.core.services.node_execution_service.run_docker", docker)
    parent_launch = asyncio.create_task(service._run_with_semaphore(parent))
    child_launch = asyncio.create_task(service._run_with_semaphore(child))
    await asyncio.wait_for(asyncio.gather(parent_launch, child_launch), 3)
    await asyncio.wait_for(asyncio.gather(*service._observers), 3)
    assert child_started.is_set()
    assert (await context.server.tasks.get_task(parent.id)).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("journal_error", [False, True], ids=["sigkill-after-start", "spawn-journal-error"])
async def test_native_handle_is_stopped_before_failure_is_recorded(server_storage, resource, monkeypatch, journal_error):
    import sys
    from simstack.runner_runtime import execution_environment, launch_claims
    entry = await acquired(server_storage)
    service = NodeExecutionService(resource, 1, 1, None)
    service.write_node_event = AsyncMock()
    monkeypatch.setattr(execution_environment, "task_launch_command",
        lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"])
    spawn = asyncio.create_subprocess_exec
    processes = []
    async def launch(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        if not journal_error:
            await context.server.tasks.start_attempt(entry, process_id=process.pid)
        return process
    monkeypatch.setattr("simstack.core.services.node_execution_service._spawn_native_process", launch)
    if journal_error:
        monkeypatch.setattr(launch_claims, "record_spawn", lambda *args: (_ for _ in ()).throw(OSError("journal disk full")))
    try:
        result = await asyncio.wait_for(service.run_node(entry), 3)
        if journal_error:
            assert not result
        else:
            assert result and processes[0].returncode is None
            processes[0].kill()
            await asyncio.wait_for(asyncio.gather(*service._observers), 3)
        assert processes[0].returncode is not None
        outcome = await context.server.tasks.get_outcome(entry)
        assert outcome.status == TaskStatus.FAILED
        if not journal_error:
            assert outcome.signal == 9
        else:
            assert "journal disk full" in outcome.error
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
async def test_restarted_service_recovers_native_outcome_without_guessed_exit(server_storage, resource, monkeypatch):
    import sys
    from simstack.runner_runtime import execution_environment, native_observations
    entry = await acquired(server_storage)
    service = NodeExecutionService(resource, 1, 1, None)
    service.write_node_event = AsyncMock()
    monkeypatch.setattr(execution_environment, "task_launch_command",
        lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"])
    original_spawn = asyncio.create_subprocess_exec
    processes = []
    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        await context.server.tasks.start_attempt(entry, process_id=process.pid)
        return process
    monkeypatch.setattr("simstack.core.services.node_execution_service._spawn_native_process", spawn)
    try:
        assert await service.run_node(entry)
        for observer in service._observers:
            observer.cancel()  # All original Runner observer state is lost.
        await asyncio.gather(*service._observers, return_exceptions=True)
        paused = asyncio.Event()
        paused.set()
        restarted = NodeExecutionService(resource, 1, 1, None, claiming_paused=paused)
        restarted.write_resource_event = AsyncMock()
        await restarted.execute()
        assert (await context.server.tasks.get_task(entry.id)).status == TaskStatus.RUNNING
        assert list(native_observations._root(context.server).glob("*.json"))
        processes[0].kill()
        await processes[0].wait()
        await restarted.execute()
        result = await context.server.tasks.get_outcome(entry)
        assert result.status == TaskStatus.FAILED
        assert result.phase == "subprocess" and result.exit_code is None and result.signal is None
        assert "unavailable after Runner restart" in result.error
        assert not list(native_observations._root(context.server).glob("*.json"))
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
async def test_live_waitpid_owner_keeps_exact_signal_during_start_poll(server_storage, resource, monkeypatch):
    import sys
    from simstack.runner_runtime import execution_environment
    entry = await acquired(server_storage)
    paused = asyncio.Event()
    paused.set()
    service = NodeExecutionService(resource, 1, 1, None, claiming_paused=paused)
    service.write_node_event = AsyncMock()
    service.write_resource_event = AsyncMock()
    monkeypatch.setattr(execution_environment, "task_launch_command",
        lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"])
    poll, release = asyncio.Event(), asyncio.Event()
    async def wait_for_start(*_):
        poll.set()
        await release.wait()
    monkeypatch.setattr(service, "_wait_for_start", wait_for_start)
    original_spawn = asyncio.create_subprocess_exec
    processes = []
    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        await context.server.tasks.start_attempt(entry, process_id=process.pid)
        return process
    monkeypatch.setattr("simstack.core.services.node_execution_service._spawn_native_process", spawn)
    launch = asyncio.create_task(service.run_node(entry))
    try:
        await asyncio.wait_for(poll.wait(), 3)
        processes[0].kill()
        await processes[0].wait()
        await service.execute()
        assert (await context.server.tasks.get_task(entry.id)).status == TaskStatus.RUNNING
        release.set()
        assert await launch
        await asyncio.gather(*service._observers)
        outcome = await context.server.tasks.get_outcome(entry)
        assert outcome.status == TaskStatus.FAILED
        assert outcome.signal == 9 if __import__("os").name != "nt" else outcome.exit_code is not None
    finally:
        release.set()
        await asyncio.gather(launch, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_remaining, ack_reply_lost", [(False, False), (True, False), (True, True)])
async def test_same_parent_waiter_cancellation_keeps_other_consumer(server_storage, monkeypatch, tmp_path, cancel_remaining, ack_reply_lost):
    import sys
    from simstack.core.node import _consumer_operations, _waiter_counts
    from simstack.core.run_node_protocol import RunNodeResult, encode_run_node_result
    parent = await acquired(server_storage)
    await context.server.tasks.start_attempt(parent)
    with task_execution(parent):
        first = Node(func=admitted_execution_probe._inner, is_async=False, parent_id=parent.id,
            parameters=Parameters(resource="self"))
        await first.get_node_registry()
        second = Node(func=admitted_execution_probe._inner, is_async=False, parent_id=parent.id,
            parameters=Parameters(resource="self"))
        await second.get_node_registry()
    assert first.id == second.id and first._owns_inline_nested_execution and not second._owns_inline_nested_execution
    spawn, processes, started = asyncio.create_subprocess_exec, [], asyncio.Event()
    release = tmp_path / "release-science"
    protocol = encode_run_node_result(RunNodeResult(True, "bool"))
    async def child_process(*args, **kwargs):
        process = await spawn(sys.executable, "-c",
            f"from pathlib import Path; import time; p=Path({str(release)!r}); "
            f"exec('while not p.exists(): time.sleep(.01)'); print({protocol!r})", **kwargs)
        processes.append(process)
        await context.server.tasks.start_attempt(first.registry_entry, process_id=process.pid)
        started.set()
        return process
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", child_process)
    first.run_node_as_process = lambda: _NATIVE_PROCESS_LAUNCH(first)
    previous_operations = set(_consumer_operations)
    initial = asyncio.create_task(first.run_somewhere())
    waiting = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        waiting = asyncio.create_task(second.run_somewhere())
        await asyncio.sleep(.05)
        own_operations = set(_consumer_operations) - previous_operations
        initial.cancel()
        with pytest.raises(asyncio.CancelledError):
            await initial
        assert processes[0].returncode is None
        assert (await context.server.tasks.get_task(first.id)).status == TaskStatus.RUNNING
        if cancel_remaining:
            if ack_reply_lost:
                original_ack = context.server.tasks.termination_confirmed
                lost = False
                async def acknowledge(entry, reason):
                    nonlocal lost
                    result = await original_ack(entry, reason)
                    if not lost:
                        lost = True
                        raise RunnerAPIError("physical stop ACK reply lost", status_code=503)
                    return result
                monkeypatch.setattr(context.server.tasks, "termination_confirmed", acknowledge)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, 3)
            assert (await context.server.tasks.get_task(first.id)).status == TaskStatus.FAILED
            if ack_reply_lost:
                from simstack.runner_runtime.launch_claims import _directory
                paths = list((_directory(context.server.requests.server_url) / "cancellations").glob("*.json"))
                assert paths and processes[0].returncode is not None
                # Parent success drains the retained exact stop proof even
                # after the physical owner's wait scope has already exited.
                await context.server.tasks.complete_attempt(parent, return_kind="bool")
                assert all(not path.exists() for path in paths)
        else:
            await context.server.tasks.complete_attempt(first.registry_entry, return_kind="bool")
            release.touch()
            assert await asyncio.wait_for(waiting, 6) is True
        await asyncio.wait_for(asyncio.gather(*own_operations, return_exceptions=True), 3)
        assert processes[0].returncode is not None
        assert not any(key[0] == str(parent.id) for key in _waiter_counts)
    finally:
        for task in (initial, waiting):
            if task is not None and not task.done():
                task.cancel()
        release.touch()
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
async def test_docker_poll_failure_retains_owner_until_its_outcome(server_storage, resource, monkeypatch):
    entry = await acquired(server_storage, parameters=Parameters(resource="test", in_docker=True))
    service = NodeExecutionService(resource, 1, 1, None)
    service.write_node_event = AsyncMock()
    started, release = asyncio.Event(), asyncio.Event()
    async def docker(task, *, on_observed_process=None, retry_unpersisted_evidence=False):
        await context.server.tasks.start_attempt(task)
        started.set()
        await release.wait()
        await context.server.tasks.complete_attempt(task, return_kind="bool")
        return True
    monkeypatch.setattr("simstack.core.services.node_execution_service.run_docker", docker)
    monkeypatch.setattr(context.server.tasks, "get_task", AsyncMock(side_effect=RunnerAPIError("poll forbidden", status_code=403)))
    fail = AsyncMock(wraps=context.server.tasks.fail_attempt)
    monkeypatch.setattr(context.server.tasks, "fail_attempt", fail)
    launch = asyncio.create_task(service.run_node(entry))
    await asyncio.wait_for(started.wait(), 3)
    assert not launch.done()
    fail.assert_not_awaited()
    release.set()
    assert await asyncio.wait_for(launch, 3)
    assert (await context.server.tasks.get_outcome(entry)).status == TaskStatus.COMPLETED
    fail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("detach", [False, True])
async def test_foreground_docker_releases_launch_capacity_before_waiting_for_child(server_storage, resource, monkeypatch, detach):
    parent = await acquired(server_storage, parameters=Parameters(resource="test", in_docker=True))
    child = await submitted(server_storage, parameters=Parameters(resource="test", in_docker=True))
    service = NodeExecutionService(resource, 1, 1, None, detach=detach)
    service.write_node_event = AsyncMock()
    child_completed = asyncio.Event()
    async def container(entry, *, on_observed_process=None, retry_unpersisted_evidence=False):
        await context.server.tasks.start_attempt(entry)
        if entry.id == parent.id:
            await child_completed.wait()
        else:
            child_completed.set()
        await context.server.tasks.complete_attempt(entry, return_kind="bool")
        return True
    monkeypatch.setattr("simstack.core.services.node_execution_service.run_docker", container)
    service._active_ids.add(str(parent.id))
    parent_task = asyncio.create_task(service._run_with_semaphore(parent))
    try:
        for _ in range(100):
            if str(parent.id) not in service._active_ids:
                break
            await asyncio.sleep(.01)
        assert str(parent.id) not in service._active_ids
        assert service._semaphore._value == 1
        assert not child_completed.is_set()
        claimed = await acquire_work(context.server, capacity=1)
        assert len(claimed) == 1 and claimed[0].id == child.id
        service._active_ids.add(str(child.id))
        assert await asyncio.wait_for(service._run_with_semaphore(claimed[0]), 2)
        assert await asyncio.wait_for(parent_task, 2)
        await asyncio.gather(*service._observers)
        assert (await context.server.tasks.get_task(parent.id)).status == TaskStatus.COMPLETED
    finally:
        child_completed.set()
        await asyncio.gather(parent_task, *service._observers, return_exceptions=True)
