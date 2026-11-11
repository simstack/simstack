"""Launch ownership across real process death and periodic queue handoff."""
import asyncio
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.models.document import ObjectId
from simstack.models.parameters import Parameters, Resource
from simstack.models.node_registry import NodeRegistry
from simstack.server_api.workflow_tasks import WorkflowTasksAPI
from simstack.runner_runtime.launch_claims import (
    finish, pending, receipt, recover_launches, reserve_launch, task_execution,
    current_execution, inherit_launch_reservation,
)


def api_client():
    requests = SimpleNamespace(server_url="https://example.invalid/api", authorization="runner", resource="local",
        bootstrap_data={"account_id": "test-account"})
    async def resolve(payload, *, request_id):
        effective = Parameters.model_validate(payload["parameters"])
        logical_self = effective.resource == "self"
        if logical_self:
            effective.resource = "local"
        return {"parameters": effective.model_dump(mode="json"),
            "logical_self": logical_self, "route": "resolved-route",
            "repository_revision": payload.get("repository_revision")}
    async def reuse(payload, *, request_id):
        return {"task": None, "disposition": "cache_miss", **await resolve(payload, request_id=request_id)}
    return SimpleNamespace(requests=requests, values=SimpleNamespace(),
        resource=SimpleNamespace(active_containers=AsyncMock(return_value=[])), tasks=SimpleNamespace(
        execution=WorkflowTasksAPI.execution,
        resolve_invocation=AsyncMock(side_effect=resolve),
        reuse_invocation=AsyncMock(side_effect=reuse),
        get_request=AsyncMock(), get_task=AsyncMock(return_value=None),
        get_outcome=AsyncMock(return_value=None), fail_attempt=AsyncMock(),
        reconcile_attempts=AsyncMock(return_value={"claims": []}),
        terminating_tasks=AsyncMock(return_value=[]), acquire_work=AsyncMock(return_value=[]),
        children=AsyncMock(return_value=[])))


def task_entry(task_id=None):
    entry = NodeRegistry(id=task_id or ObjectId(), name="launch_probe",
        status=TaskStatus.RETRIEVED, function_hash="", arg_hash="hash",
        func_mapping="test.launch_probe", parameters=Parameters(resource="local"))
    object.__setattr__(entry, "_execution_claim_id", "original-claim")
    object.__setattr__(entry, "_execution_generation", "generation-one")
    object.__setattr__(entry, "_execution_resource", "local")
    return entry


@pytest.mark.asyncio
async def test_claims_and_acquisition_are_scoped_to_stable_account(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    from simstack.runner_runtime.launch_claims import acquire_work, _receipt_path
    owner, other = api_client(), api_client()
    other.requests.bootstrap_data = {"account_id": "another-account"}
    task = task_entry()
    value = receipt(owner, task.id, begin=True, generation=task._execution_generation,
        claim_id=task._execution_claim_id)
    assert _receipt_path(owner, task.id) != _receipt_path(other, task.id)
    # Even a copied task/claim identity is independently owned in another workspace.
    receipt(other, task.id, begin=True, generation=task._execution_generation, claim_id=task._execution_claim_id)
    assert _receipt_path(owner, task.id).is_file()
    other.tasks.reconcile_attempts.return_value = {"claims": [{"state": "stale"}]}
    assert await recover_launches(other) == []
    assert _receipt_path(owner, task.id).is_file()
    assert list(pending(owner)) == [value]
    owner.tasks.acquire_work.side_effect = RuntimeError("reply lost")
    with pytest.raises(RuntimeError, match="reply lost"):
        await acquire_work(owner, 1)
    request = owner.tasks.acquire_work.await_args.kwargs["request_id"]
    other.tasks.acquire_work.return_value = []
    await acquire_work(other, 1)
    assert other.tasks.acquire_work.await_args.kwargs["request_id"] != request
    owner.requests.token = "rotated-token"
    owner.tasks.acquire_work.side_effect = None
    owner.tasks.acquire_work.return_value = []
    await acquire_work(owner, 1)
    assert owner.tasks.acquire_work.await_args.kwargs["request_id"] == request


def test_claim_without_bootstrapped_account_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    api.requests.bootstrap_data = None
    with pytest.raises(ValueError, match="bootstrapped account"):
        receipt(api, ObjectId(), begin=True)


def test_bootstrapped_resource_named_self_keeps_its_claim_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    api.requests.resource = "self"
    value = receipt(api, ObjectId(), begin=True)
    assert value["actor"]["resource"] == "self"
    assert value["actor"]["account_id"] == "test-account"


def test_slurm_submission_intents_do_not_cross_accounts_with_same_task_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    from simstack.runner_runtime.slurm_submission_intents import begin_intent, pending_intents, read_intent
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    owner, other = api_client(), api_client()
    other.requests.bootstrap_data = {"account_id": "another-account"}
    owner.tasks.execution = other.tasks.execution = WorkflowTasksAPI.execution
    entry = task_entry()
    entry._execution_start_id = "original-start"
    first = begin_intent(owner, entry)
    assert list(pending_intents(other, "local")) == []
    assert read_intent(other, entry.id) is None
    second = begin_intent(other, entry)
    assert second["submission_id"] != first["submission_id"]
    assert list(pending_intents(owner, "local")) == [first]
    assert list(pending_intents(other, "local")) == [second]


@pytest.mark.asyncio
async def test_live_publisher_cannot_be_recovered_and_pending_publication_survives(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    task_id = str(ObjectId())
    reservation = reserve_launch(api, task_id)
    intent = receipt(api, task_id, begin=True, inline=True,
        publication={"requested_task_id": task_id, "parent_execution": {"task_id": str(ObjectId())}}, request_id="publication-request")
    assert await recover_launches(api) == []
    api.tasks.get_request.assert_not_awaited()
    reservation.close()
    api.tasks.get_request.return_value = {"state": "applied", "result": {
        "disposition": "launch_inline", "task": task_entry(ObjectId(task_id))}}
    assert await recover_launches(api) == []
    assert list(pending(api)) == [intent]
    assert api.tasks.get_request.await_args.args[0] == "publication-request"


@pytest.mark.asyncio
async def test_dead_publisher_late_commit_recovered_without_releasing_executor_reservation(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    task_id = str(ObjectId())
    code = '''
from types import SimpleNamespace
from simstack.runner_runtime.launch_claims import receipt, reserve_launch
from simstack.models.document import ObjectId
api = SimpleNamespace(requests=SimpleNamespace(server_url="https://example.invalid/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
reservation = reserve_launch(api, TASK_ID)
receipt(api, TASK_ID, begin=True, inline=True,
        publication={"requested_task_id": TASK_ID, "parent_execution": {"task_id": str(ObjectId())}}, request_id="late-publication")
print("reserved", flush=True)
import time
time.sleep(60)
'''.replace("TASK_ID", repr(task_id))
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        assert await asyncio.to_thread(process.stdout.readline) == "reserved\n"
        assert await recover_launches(api) == []
        api.tasks.get_request.assert_not_awaited()
        process.kill()
        await asyncio.to_thread(process.wait)
        api.tasks.get_request.return_value = {"state": "applied", "result": {
        "disposition": "launch_inline", "task": task_entry(ObjectId(task_id))}}
        assert await recover_launches(api) == []
        assert list(pending(api))[0]["request_id"] == "late-publication"
        entry = task_entry(ObjectId(task_id))
        api.tasks.get_task.return_value = entry
        recovered = await recover_launches(api)
        assert recovered == [entry]
        assert await recover_launches(api) == []
        assert api.tasks.get_request.await_count == 2
        entry._launch_reservation.close()
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.asyncio
async def test_recovered_reservation_covers_busy_semaphore_and_periodic_polls(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    entry = task_entry()
    value = receipt(api, entry.id, begin=True, claim_id="original-claim", generation="generation-one")
    finish(api, value, claimed=True)
    api.tasks.reconcile_attempts.return_value = {"claims": [{"state": "recoverable", "task": entry}]}
    monkeypatch.setattr("simstack.core.services.node_execution_service.context", SimpleNamespace(server=api))
    service = NodeExecutionService(Resource(value="local"), 1, 1, None)
    service._started = True
    service.write_resource_event = AsyncMock()
    service.write_node_event = AsyncMock()
    service.run_node = AsyncMock(return_value=True)
    await service._semaphore.acquire()
    await service.execute()
    await asyncio.sleep(0)
    assert str(entry.id) in service._active_ids
    await service.execute()
    await service.execute()
    api.tasks.reconcile_attempts.assert_awaited_once()
    service.run_node.assert_not_awaited()
    assert len(service._running_tasks) == 1
    service._semaphore.release()
    await asyncio.gather(*service._running_tasks)
    service.run_node.assert_awaited_once()
    assert not service._active_ids


@pytest.mark.asyncio
async def test_reservation_covers_slow_preparation_and_child_start_handshake(monkeypatch, tmp_path):
    import simstack.core.services.node_execution_service as execution_service
    from simstack.runner_runtime import execution_environment
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    entry = task_entry()
    value = receipt(api, entry.id, begin=True, claim_id="original-claim", generation="generation-one")
    finish(api, value, claimed=True)
    api.tasks.reconcile_attempts.return_value = {"claims": [{"state": "recoverable", "task": entry}]}
    preparing, prepared, bootstrapping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def prepare(_):
        preparing.set()
        await prepared.wait()
    canonical = entry.model_copy(deep=True)
    async def read_task(_):
        bootstrapping.set()
        return canonical
    api.tasks.get_task.side_effect = read_task
    active = SimpleNamespace(server=api, config=SimpleNamespace(resource="local", project_root=tmp_path, python_paths=[]))
    monkeypatch.setattr(execution_service, "context", active)
    monkeypatch.setattr(execution_environment, "prepare_task", prepare)
    monkeypatch.setattr(execution_environment, "task_launch_command", lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(execution_environment, "task_environment", lambda *args, **kwargs: dict(os.environ))
    monkeypatch.setattr(execution_service, "record_process_id", AsyncMock())
    service = NodeExecutionService(Resource(value="local"), 1, 1, None, detach=True)
    service._started = True
    service.write_resource_event = AsyncMock()
    service.write_node_event = AsyncMock()
    spawned = []
    original_spawn = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr(execution_service, "_spawn_native_process", spawn)
    try:
        await service.execute()
        await asyncio.wait_for(preparing.wait(), 5)
        await service.execute()
        await service.execute()
        assert reserve_launch(api, entry.id) is None and not spawned
        prepared.set()
        await asyncio.wait_for(bootstrapping.wait(), 5)
        await service.execute()
        await service.execute()
        assert reserve_launch(api, entry.id) is None and len(spawned) == 1
        api.tasks.reconcile_attempts.assert_awaited_once()
        object.__setattr__(canonical, "_execution_start_id", entry._execution_start_id)
        canonical.status = TaskStatus.RUNNING
        await asyncio.wait_for(asyncio.gather(*service._running_tasks), 5)
        assert not list(pending(api))
        # This stub never performs the child's start ACK, so it still owns its
        # inherited reservation even after the launcher saw the Server state.
        assert reserve_launch(api, entry.id) is None
        # A real failure response is a task snapshot, without a native START
        # marker for this sleep-only stub (a bare AsyncMock invents attributes).
        api.tasks.fail_attempt.return_value = entry.model_copy(update={
            "status": TaskStatus.FAILED, "error": "Stub scientific process stopped"})
        spawned[0].terminate()
        await spawned[0].wait()
        reservation = reserve_launch(api, entry.id)
        assert reservation is not None
        reservation.close()
    finally:
        prepared.set()
        for task in service._running_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*service._running_tasks, return_exceptions=True)
        for process in spawned:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        await asyncio.gather(*service._observers)


def test_execution_identity_is_scoped_and_retains_original_start(monkeypatch):
    for name in ("SIMSTACK_EXECUTION_TASK", "SIMSTACK_EXECUTION_CLAIM", "SIMSTACK_EXECUTION_START"):
        monkeypatch.delenv(name, raising=False)
    parent = task_entry()
    child = task_entry()
    with task_execution(parent):
        parent_identity = dict(current_execution())
        with task_execution(child):
            child_identity = dict(current_execution())
            assert child_identity["task_id"] == str(child.id)
            assert child_identity["generation"] == "generation-one"
        assert current_execution() == parent_identity
    with task_execution(child):
        assert current_execution() == child_identity


@pytest.mark.asyncio
async def test_outcomes_reconcile_even_when_admission_is_paused(monkeypatch, tmp_path):
    api = api_client()
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    from simstack.runner_runtime.pending_execution_failures import record_failure
    record_failure("bootstrap failed", phase="bootstrap", server=api, resource="local", execution={
        "task_id": str(ObjectId()), "claim_id": "claim", "start_id": "start"})
    monkeypatch.setattr("simstack.core.services.node_execution_service.context", SimpleNamespace(server=api))
    paused = asyncio.Event()
    paused.set()
    service = NodeExecutionService(Resource(value="local"), 1, 1, None, claiming_paused=paused)
    service._started = True
    await service.execute()
    api.tasks.reconcile_attempts.assert_awaited_once()
    api.tasks.acquire_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_publication_is_reserved_before_request_and_runs_at_capacity_one(monkeypatch, tmp_path):
    from simstack.core.node import Node
    from simstack.models.base_types import BooleanData
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    from simstack.runner_runtime import launch_claims as claims
    write_receipt = claims._write
    written = set()
    def durable_replace(path, value):
        if path in written:
            assert path.exists(), "Publication intent disappeared before its canonical claim was durable"
        write_receipt(path, value)
        written.add(path)
    monkeypatch.setattr(claims, "_write", durable_replace)
    api = api_client()
    value = BooleanData(value=True)
    input_publication = str(uuid4())
    api.values.publish_task_inputs = AsyncMock(return_value=([value], input_publication))
    mapping = SimpleNamespace(function_mapping="test.probe", repository_revision="revision-one")
    model_mapping = SimpleNamespace(mapping="simstack.models.base_types.BooleanData")
    active = SimpleNamespace(server=api, in_docker=False,
        config=SimpleNamespace(resource="local", project_root=tmp_path),
        node_mappings=SimpleNamespace(get_by_name=lambda name: mapping),
        model_mappings=SimpleNamespace(get_by_name=lambda name: model_mapping))
    monkeypatch.setattr("simstack.core.node.context", active)
    monkeypatch.setattr("simstack.core.context.context", active)
    parent = task_entry()
    observed = []

    async def publish(payload, *, request_id):
        assert payload["input_publication"] == input_publication
        intent = list(pending(api))[0]
        assert intent["publication"] == payload
        assert intent["request_id"] == request_id
        assert reserve_launch(api, payload["requested_task_id"]) is None
        observed.append(payload)
        return {"disposition": "launch_inline", "recompute_artifacts": False, "task": task_entry(ObjectId(payload["requested_task_id"]))}

    api.tasks.create_child = publish
    def probe(input_model, **kwargs):
        return input_model
    node = Node(value, func=probe, is_async=False, parent_id=parent.id,
        parameters=Parameters(resource="self"))
    service = NodeExecutionService(Resource(value="local"), 1, 1, None)
    await service._semaphore.acquire()  # parent occupies the only ordinary slot
    with task_execution(parent):
        await node.get_node_registry()
    api.values.publish_task_inputs.assert_awaited_once_with([value], repository_revision=None)
    async def run_child():
        node.registry_entry.status = TaskStatus.COMPLETED
        return True
    node.run_node_as_process = run_child
    assert await asyncio.wait_for(node.run_somewhere(), timeout=1) is True
    service._semaphore.release()
    assert len(observed) == 1
    handle = reserve_launch(api, node.id)
    assert handle is not None
    handle.close()


@pytest.mark.asyncio
async def test_ambiguous_publication_keeps_exact_intent_for_recovery(monkeypatch, tmp_path):
    from simstack.core.node import Node
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    api.requests.authorization = "user"
    input_publication = str(uuid4())
    api.values.publish_task_inputs = AsyncMock(return_value=([], input_publication))
    api.tasks.create_child = AsyncMock(side_effect=OSError("response lost"))
    mapping = SimpleNamespace(function_mapping="test.probe", repository_revision="revision-one")
    active = SimpleNamespace(server=api, in_docker=False,
        config=SimpleNamespace(resource="local", project_root=tmp_path),
        node_mappings=SimpleNamespace(get_by_name=lambda name: mapping))
    monkeypatch.setattr("simstack.core.node.context", active)
    parent = task_entry()
    def probe(**kwargs):
        return True
    node = Node(func=probe, is_async=False, parent_id=parent.id,
        parameters=Parameters(resource="self"))
    with task_execution(parent), pytest.raises(OSError, match="response lost"):
        await node.get_node_registry()
    intent = list(pending(api))[0]
    assert intent["publication"]["input_publication"] == input_publication
    api.values.publish_task_inputs.assert_awaited_once_with([], repository_revision=None)
    assert intent["publication"] == api.tasks.create_child.await_args.args[0]
    assert intent["request_id"] == api.tasks.create_child.await_args.kwargs["request_id"]
    handle = reserve_launch(api, intent["task_id"])
    assert handle is not None  # dead publisher's recovery can take ownership
    handle.close()


@pytest.mark.asyncio
async def test_user_root_submission_does_not_enter_runner_claim_recovery(monkeypatch, tmp_path):
    from simstack.core.node import Node
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    api.requests.authorization = "user"
    input_publication = str(uuid4())
    api.values.publish_task_inputs = AsyncMock(return_value=([], input_publication))
    api.tasks.create_child = AsyncMock(side_effect=AssertionError("root used Runner child authority"))
    async def submit(payload, *, request_id):
        assert payload["input_publication"] == input_publication
        return {"disposition": "scheduled", "recompute_artifacts": False, "task": NodeRegistry(
            id=payload["requested_task_id"], name="root_probe", status=TaskStatus.SUBMITTED,
            func_mapping="test.root_probe", function_hash="", arg_hash="hash",
            parameters=Parameters(resource="local"))}
    api.tasks.submit_root = AsyncMock(side_effect=submit)
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=api,
        config=SimpleNamespace(resource="local"), in_docker=False, current_node_name=None,
        node_mappings=SimpleNamespace(get_by_name=lambda name: SimpleNamespace(function_mapping="test.root_probe"))))
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: False)
    def root_probe(**kwargs):
        return True
    invocation = Node(func=root_probe, is_async=False, parameters=Parameters())
    await invocation.get_node_registry()
    api.values.publish_task_inputs.assert_awaited_once_with([], repository_revision=None)
    api.tasks.submit_root.assert_awaited_once()
    api.tasks.create_child.assert_not_awaited()
    assert list(pending(api)) == []


@pytest.mark.asyncio
async def test_lost_acquisition_replays_original_request_before_claiming_more(monkeypatch, tmp_path):
    from simstack.runner_runtime.launch_claims import acquire_work
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    entry = task_entry()
    server.tasks.acquire_work.side_effect = [OSError("reply lost"), [entry]]
    with pytest.raises(OSError, match="reply lost"):
        await acquire_work(server, capacity=2)
    assert list(pending(server)) == []
    assert await acquire_work(server, capacity=9) == [entry]
    first, replay = server.tasks.acquire_work.await_args_list
    assert first == replay
    assert first.args == (2,)
    assert list(pending(server))[0]["claim_id"] == entry._execution_claim_id
    from simstack.runner_runtime.launch_claims import _directory
    assert not list(_directory(server.requests.server_url).glob("work-acquisition-*.json"))


@pytest.mark.asyncio
async def test_shared_claim_journals_skip_foreign_and_old_records_before_rpc(monkeypatch, tmp_path, caplog):
    from simstack.runner_runtime.launch_claims import _directory, _write
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    other = api_client()
    other.requests.resource = "other"
    foreign = receipt(other, ObjectId(), begin=True)
    local = api_client()
    local.requests.authorization = "local"
    local.requests.token = "foreign-local-capability"
    scoped = receipt(local, ObjectId(), begin=True)
    old_id = str(ObjectId())
    directory = _directory(other.requests.server_url)
    old = directory / (old_id + ".json")
    _write(old, {"task_id": old_id, "state": "pending", "claim_id": "old"})
    broken = directory / "unreadable.json"
    broken.write_text("{")
    own = api_client()
    value = receipt(own, ObjectId(), begin=True)
    own.tasks.reconcile_attempts.return_value = {"claims": [{"state": "terminal"}]}
    assert await recover_launches(own) == []
    own.tasks.reconcile_attempts.assert_awaited_once_with(claims=[value], outcomes=[])
    assert old.exists() and broken.exists()
    assert {item["task_id"] for item in pending(own)} == {foreign["task_id"], scoped["task_id"], old_id}
    assert "Unscoped or unknown launch journal" in caplog.text
    assert "Unreadable launch journal" in caplog.text
    assert "foreign-local-capability" not in json.dumps(scoped)
    local.tasks.reconcile_attempts.return_value = {"claims": [{"state": "terminal"}]}
    local.requests.token = "different-local-capability"
    assert await recover_launches(local) == []
    local.tasks.reconcile_attempts.assert_not_awaited()
    local.requests.token = "foreign-local-capability"
    assert await recover_launches(local) == []
    local.tasks.reconcile_attempts.assert_awaited_once_with(claims=[scoped], outcomes=[])


@pytest.mark.asyncio
async def test_own_claim_authorization_failure_is_retained_and_propagates(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    value = receipt(server, ObjectId(), begin=True)
    server.tasks.reconcile_attempts.side_effect = PermissionError("own claim denied")
    with pytest.raises(PermissionError, match="own claim denied"):
        await recover_launches(server)
    assert list(pending(server)) == [value]
    reservation = reserve_launch(server, value["task_id"])
    assert reservation is not None
    reservation.close()


@pytest.mark.asyncio
async def test_shared_acquisition_intents_keep_each_resource_original_request(monkeypatch, tmp_path, caplog):
    from simstack.runner_runtime.launch_claims import _directory, _write, acquire_work
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    first, second = api_client(), api_client()
    second.requests.resource = "other"
    old = _directory(first.requests.server_url) / "work-acquisition.json"
    _write(old, {"request_id": "unknown-old-owner", "capacity": 10})
    first.tasks.acquire_work.side_effect = [OSError("reply lost"), []]
    with pytest.raises(OSError, match="reply lost"):
        await acquire_work(first, capacity=2)
    await acquire_work(second, capacity=4)
    await acquire_work(first, capacity=9)
    original, replay = first.tasks.acquire_work.await_args_list
    assert original == replay
    assert original.args == (2,)
    assert second.tasks.acquire_work.await_args.args == (4,)
    assert second.tasks.acquire_work.await_args.kwargs["request_id"] != original.kwargs["request_id"]
    assert old.exists() and "Unscoped work acquisition journal retained" in caplog.text


def test_claim_receipt_requires_resource_before_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    server.requests.resource = "self"
    server.requests.bootstrap_data = None
    with pytest.raises(ValueError, match="bootstrapped account and resource"):
        receipt(server, ObjectId(), begin=True)


@pytest.mark.asyncio
async def test_pending_publication_replays_the_exact_child_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    entry = task_entry()
    publication = {"requested_task_id": str(entry.id), "parent_execution": {"task_id": str(ObjectId())}}
    intent = receipt(server, entry.id, begin=True, inline=True,
        publication=publication, request_id="original-publication")
    server.tasks.get_request.return_value = {"state": "pending"}
    server.tasks.create_child = AsyncMock(return_value={"task": entry, "disposition": "launch_inline"})
    server.tasks.get_task.return_value = entry
    recovered = await recover_launches(server)
    assert recovered == [entry]
    server.tasks.create_child.assert_awaited_once_with(publication, request_id=intent["request_id"])
    assert reserve_launch(server, entry.id) is None
    entry._launch_reservation.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "applied"])
async def test_root_publication_recovery_uses_original_user_request_and_scoped_credential(monkeypatch, tmp_path, state):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    server.requests.authorization = "user"
    entry = task_entry()
    object.__setattr__(entry, "_execution_local", True)
    publication = {"requested_task_id": str(entry.id), "parent_execution": None, "inline": True}
    intent = receipt(server, entry.id, begin=True, inline=True,
        publication=publication, request_id="original-root-publication")
    response = {"task": entry, "disposition": "launch_inline", "local_execution_token": "task-scoped-credential"}
    server.tasks.get_request.return_value = {"state": state, "result": response} if state == "applied" else {"state": state}
    server.tasks.submit_root = AsyncMock(return_value=response)
    server.tasks.create_child = AsyncMock(side_effect=AssertionError("root replay used child API"))
    server.tasks.get_task.return_value = entry
    recovered = await recover_launches(server)
    assert recovered == [entry]
    assert entry._execution_token == "task-scoped-credential"
    server.tasks.create_child.assert_not_awaited()
    if state == "pending":
        server.tasks.submit_root.assert_awaited_once_with(publication, request_id=intent["request_id"])
    else:
        server.tasks.submit_root.assert_not_awaited()
    assert reserve_launch(server, entry.id) is None
    entry._launch_reservation.close()


def test_receipt_does_not_implicitly_grant_execution_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    for key in ("TASK", "GENERATION", "CLAIM", "START"):
        monkeypatch.delenv("SIMSTACK_EXECUTION_" + key, raising=False)
    server = api_client()
    task_id = ObjectId()
    value = receipt(server, task_id, begin=True, claim_id="stored-claim", generation="stored-generation")
    finish(server, value, claimed=True)
    with task_execution(SimpleNamespace(id=task_id)):
        assert current_execution() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("acknowledge", [False, True])
async def test_live_child_keeps_reservation_after_launcher_dies(monkeypatch, tmp_path, recorded, acknowledge):
    """Hard death on either side of record_spawn cannot expose a live launch."""
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    entry = task_entry()
    task_id = str(entry.id)
    child_code = '''
import os, time
from pathlib import Path
from types import SimpleNamespace
from simstack.runner_runtime.launch_claims import acknowledge_start
root = Path(os.environ["SIMSTACK_RUNNER_WORKDIR"])
while not (root / "child-release").exists():
    if (root / "child-start").exists() and not (root / "child-acknowledged").exists():
        execution = {field: os.environ["SIMSTACK_EXECUTION_" + key] for field, key in
            (("task_id", "TASK"), ("claim_id", "CLAIM"), ("start_id", "START"))}
        server = SimpleNamespace(requests=SimpleNamespace(server_url="https://example.invalid/api",
            authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}),
            tasks=SimpleNamespace(execution=lambda _: execution))
        acknowledge_start(server, SimpleNamespace(id=execution["task_id"]), True)
        (root / "child-acknowledged").touch()
    time.sleep(0.01)
'''
    launcher_code = '''
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from simstack.models import NodeRegistry
from simstack.models.parameters import Parameters, Resource
from simstack.runner_runtime import execution_environment, launch_claims
from simstack.core.services import node_execution_service
root = Path(os.environ["SIMSTACK_RUNNER_WORKDIR"])
entry = NodeRegistry(id=TASK_ID, name="launch_probe", status="retrieved",
    function_hash="", arg_hash="hash", func_mapping="test.launch_probe", parameters=Parameters(resource="local"))
entry._execution_claim_id = "original-claim"
entry._execution_generation = "generation-one"
entry._execution_resource = "local"
server = SimpleNamespace(requests=SimpleNamespace(server_url="https://example.invalid/api",
    authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
reservation = launch_claims.reserve_launch(server, entry.id)
intent = launch_claims.receipt(server, entry.id, begin=True,
    claim_id="original-claim", generation="generation-one")
launch_claims.finish(server, intent, claimed=True)
entry._launch_reservation = reservation
node_execution_service.context = SimpleNamespace(server=server,
    config=SimpleNamespace(resource="local", project_root=root, python_paths=[]))
execution_environment.prepare_task = AsyncMock()
execution_environment.task_launch_command = lambda *args: [sys.executable, "-c", CHILD_CODE]
original = launch_claims.record_spawn
def crash_after_spawn(api, entry, pid):
    if RECORDED:
        original(api, entry, pid)
    (root / "child-pid").write_text(str(pid))
    os._exit(0)
launch_claims.record_spawn = crash_after_spawn
service = node_execution_service.NodeExecutionService(Resource(value="local"), 1, 1, None)
service.write_node_event = AsyncMock()
asyncio.run(service._run_with_semaphore(entry))
'''.replace("TASK_ID", repr(task_id)).replace("CHILD_CODE", repr(child_code)).replace("RECORDED", repr(recorded))
    launcher = subprocess.Popen([sys.executable, "-c", launcher_code], env=dict(os.environ),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child_pid = None
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.to_thread(launcher.communicate), 10)
        assert launcher.returncode == 0, stdout + stderr
        child_pid = int((tmp_path / "child-pid").read_text())
        os.kill(child_pid, 0)
        assert bool(list(pending(server))[0].get("process_id")) == recorded
        assert await recover_launches(server) == []
        server.tasks.reconcile_attempts.assert_not_awaited()
        assert reserve_launch(server, task_id) is None
        if acknowledge:
            (tmp_path / "child-start").touch()
            deadline = time.monotonic() + 5
            while not (tmp_path / "child-acknowledged").exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert (tmp_path / "child-acknowledged").exists()
            os.kill(child_pid, 0)
            assert not list(pending(server))
        else:
            (tmp_path / "child-release").touch()
        deadline = time.monotonic() + 5
        available = None
        while available is None and time.monotonic() < deadline:
            available = reserve_launch(server, task_id)
            if available is None:
                await asyncio.sleep(0.01)
        assert available is not None
        available.close()
        if not acknowledge:
            server.tasks.reconcile_attempts.return_value = {"claims": [{"state": "claimed", "task": entry}]}
            recovered = await recover_launches(server)
            assert recovered == [entry]
            entry._launch_reservation.close()
    finally:
        (tmp_path / "child-release").touch()
        if launcher.poll() is None:
            launcher.kill()
            await asyncio.to_thread(launcher.wait)
        if child_pid is not None:
            try:
                os.kill(child_pid, 15)
            except ProcessLookupError:
                pass


def test_inherited_reservation_only_follows_the_same_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    reservation = reserve_launch(server, ObjectId())
    environment = dict(os.environ)
    environment.update(SIMSTACK_EXECUTION_TASK=str(ObjectId()), SIMSTACK_EXECUTION_GENERATION="generation",
        SIMSTACK_EXECUTION_CLAIM="claim", SIMSTACK_EXECUTION_START="start")
    try:
        with inherit_launch_reservation(reservation, environment) as initial:
            for key in ("SIMSTACK_LAUNCH_RESERVATION", "SIMSTACK_EXECUTION_TASK", "SIMSTACK_EXECUTION_GENERATION",
                        "SIMSTACK_EXECUTION_CLAIM", "SIMSTACK_EXECUTION_START"):
                monkeypatch.setenv(key, environment[key])
            with inherit_launch_reservation(None, dict(environment)) as forwarded:
                if os.name == "nt":
                    assert forwarded["startupinfo"].lpAttributeList == initial["startupinfo"].lpAttributeList
                else:
                    assert forwarded == initial
        other = {**environment, "SIMSTACK_EXECUTION_START": "another-start"}
        with inherit_launch_reservation(None, other) as ownership:
            assert ownership == {} and "SIMSTACK_LAUNCH_RESERVATION" not in other
        monkeypatch.delenv("SIMSTACK_EXECUTION_CLAIM")
        with inherit_launch_reservation(None, dict(environment)) as ownership:
            assert ownership == {}
    finally:
        reservation.close()


def test_parent_spawn_receipt_cannot_overwrite_child_container_identity(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from simstack.runner_runtime import launch_claims
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI

    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server, entry = api_client(), task_entry()
    server.tasks.execution = WorkflowTasksAPI.execution
    entry._execution_start_id = "original-start"
    receipt(server, entry.id, begin=True, claim_id=entry._execution_claim_id,
        generation=entry._execution_generation)
    writing_spawn, release_spawn, writing_container = Event(), Event(), Event()
    write = launch_claims._write
    def stalled_write(path, value):
        if value.get("process_id") == 1234 and "container_name" not in value:
            writing_spawn.set()
            assert release_spawn.wait(5)
        write(path, value)
        if "container_name" in value:
            writing_container.set()
    monkeypatch.setattr(launch_claims, "_write", stalled_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        spawned = pool.submit(launch_claims.record_spawn, server, entry, 1234)
        try:
            assert writing_spawn.wait(5)
            container = pool.submit(launch_claims.record_container_launch, server, entry, "owned-container")
            assert not writing_container.wait(0.05)
        finally:
            release_spawn.set()
        spawned.result(timeout=5)
        container.result(timeout=5)
    saved = receipt(server, entry.id)
    assert saved["process_id"] == 1234 and saved["container_name"] == "owned-container"
    assert saved["start_id"] == "original-start"


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("SIMSTACK_TEST_DOCKER_IMAGE"), reason="Explicit local Docker image required")
async def test_docker_cli_keeps_inherited_reservation_after_intermediate_python_dies(monkeypatch, tmp_path):
    """Exercise the real host Python -> Docker CLI handoff, without scientific code."""
    from uuid import uuid4
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    server = api_client()
    entry = task_entry()
    container = "simstack-claim-probe-" + uuid4().hex
    image = os.environ["SIMSTACK_TEST_DOCKER_IMAGE"]
    command = ["docker", "run", "--name", container, "--cidfile", str(tmp_path / "container-id"),
        "--entrypoint", "sh", image, "-c", "sleep 60"]
    intermediate = '''
import os, subprocess
from pathlib import Path
from simstack.runner_runtime.launch_claims import inherit_launch_reservation
environment = dict(os.environ)
with inherit_launch_reservation(None, environment) as ownership:
    child = subprocess.Popen(COMMAND, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **ownership)
Path(os.environ["SIMSTACK_RUNNER_WORKDIR"], "docker-pid").write_text(str(child.pid))
os._exit(0)
'''.replace("COMMAND", repr(command))
    launcher_code = '''
import os, subprocess, sys
from types import SimpleNamespace
from simstack.runner_runtime.launch_claims import reserve_launch, receipt, finish, inherit_launch_reservation
server = SimpleNamespace(requests=SimpleNamespace(server_url="https://example.invalid/api", resource="local", authorization="runner", bootstrap_data={"account_id": "test-account"}))
reservation = reserve_launch(server, TASK_ID)
intent = receipt(server, TASK_ID, begin=True, generation="generation-one", claim_id="original-claim")
finish(server, intent, claimed=True)
environment = dict(os.environ, SIMSTACK_EXECUTION_TASK=TASK_ID, SIMSTACK_EXECUTION_GENERATION="generation-one",
    SIMSTACK_EXECUTION_CLAIM="original-claim", SIMSTACK_EXECUTION_START="first-start")
with inherit_launch_reservation(reservation, environment) as ownership:
    process = subprocess.Popen([sys.executable, "-c", INTERMEDIATE], env=environment, **ownership)
process.wait()
os._exit(process.returncode)
'''.replace("TASK_ID", repr(str(entry.id))).replace("INTERMEDIATE", repr(intermediate))
    launcher = subprocess.Popen([sys.executable, "-c", launcher_code], env=dict(os.environ),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    docker_pid = None
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.to_thread(launcher.communicate), 10)
        assert launcher.returncode == 0, stdout + stderr
        docker_pid = int((tmp_path / "docker-pid").read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            observed = await asyncio.to_thread(subprocess.run,
                ["docker", "inspect", "--format", "{{.State.Running}}", container], capture_output=True, text=True)
            if observed.returncode == 0 and observed.stdout.strip() == "true":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("Local container did not start")
        os.kill(docker_pid, 0)
        assert reserve_launch(server, entry.id) is None
        assert await recover_launches(server) == []
        server.tasks.reconcile_attempts.assert_not_awaited()
    finally:
        await asyncio.to_thread(subprocess.run, ["docker", "rm", "--force", container], capture_output=True)
        if launcher.poll() is None:
            launcher.kill()
            await asyncio.to_thread(launcher.wait)
        if docker_pid is not None:
            try:
                os.kill(docker_pid, 15)
            except ProcessLookupError:
                pass


@pytest.fixture
def docker_claim(monkeypatch, tmp_path):
    """Use a real isolated daemon container; only the Server task store is fake."""
    from uuid import uuid4
    from simstack.core import run_docker
    from simstack.core.services import task_termination
    from simstack.runner_runtime.launch_claims import record_container_launch
    from simstack.server_api.workflow_tasks import WorkflowTasksAPI
    from simstack.util.resource_config import ResourceConfig

    image = os.environ.get("SIMSTACK_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("Explicit local Docker image required")
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    api.requests.bootstrap_data = {"account_id": "docker-acceptance-account"}
    api.tasks.execution = WorkflowTasksAPI.execution
    api.tasks.record_process = AsyncMock()
    entry = task_entry()
    entry.parameters.in_docker = True
    entry._execution_start_id = str(uuid4())
    api.tasks.get_outcome = AsyncMock(return_value=entry)
    api.tasks.admit_container = AsyncMock(return_value=entry)
    api.tasks.termination_confirmed = AsyncMock(return_value=entry)
    async def fail(launch, error, **kwargs):
        launch.status = TaskStatus.FAILED
        launch.error = launch.error or error
        return launch
    api.tasks.fail_attempt = AsyncMock(side_effect=fail)
    intent = receipt(api, entry.id, begin=True, claim_id=entry._execution_claim_id, generation=entry._execution_generation)
    finish(api, intent, claimed=True)
    name = run_docker.docker_execution_name(api, api.tasks.execution(entry))
    record_container_launch(api, entry, name)
    task_dir = run_docker.ensure_host_task_workdir(tmp_path, entry.name, str(entry.id))
    cidfile = run_docker.docker_cidfile_path(task_dir)
    (tmp_path / "config.toml").write_text(f'[local.program.launch_probe]\ndocker_image="{image}"\n')
    active = SimpleNamespace(server=api, resource_config=ResourceConfig(tmp_path, "local"),
        config=SimpleNamespace(resource="local", workdir=tmp_path, project_root=tmp_path, python_paths=[]))
    monkeypatch.setattr(run_docker, "context", active)
    monkeypatch.setattr(task_termination, "context", active)
    monkeypatch.setattr(run_docker, "_harmonize_docker_task_dir", AsyncMock())
    owned = [name]
    value = SimpleNamespace(api=api, entry=entry, name=name, image=image, cidfile=cidfile, owned=owned)
    try:
        yield value
    finally:
        for container in owned:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=15)


def _docker_python_command(claim, directory, *, action="create", name=None, marker="starts", release="release"):
    script = ("from pathlib import Path; import time; p=Path('/probe'); "
        f"(p/{marker!r}).open('a').write('started\\n'); "
        f"exec(\"while not (p/{release!r}).exists(): time.sleep(0.01)\"); raise SystemExit(17)")
    name = name or claim.name
    return ["docker", action, "--name", name, "--label", "simstack.execution=" + name,
        "--user", "0:0", "--entrypoint", "python", "--mount", f"type=bind,source={directory},target=/probe",
        claim.image, "-c", script]


@pytest.mark.asyncio
@pytest.mark.parametrize("late_creation", [False, True])
async def test_recovered_created_container_resumes_exact_execution(docker_claim, monkeypatch, tmp_path, late_creation):
    from simstack.core import run_docker
    claim = docker_claim
    planned_execution = claim.api.tasks.execution(claim.entry).copy()
    subprocess.run(_docker_python_command(claim, tmp_path), check=True, capture_output=True, timeout=15)
    (tmp_path / "release").touch()
    actual_inspect = run_docker.inspect_docker_execution
    if late_creation:
        observations = [None]
        def observe(name):
            return observations.pop() if observations else actual_inspect(name)
        monkeypatch.setattr(run_docker, "inspect_docker_execution", observe)
    commands = []
    spawn = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        if args[:2] in {("docker", "run"), ("docker", "start")}:
            claim.api.tasks.admit_container.assert_awaited_once()
            admitted = claim.api.tasks.admit_container.await_args.args[0]
            assert claim.api.tasks.execution(admitted) == planned_execution
        commands.append(args)
        return await spawn(*args, **kwargs)
    monkeypatch.setattr(run_docker.asyncio, "create_subprocess_exec", capture)
    outcome = await asyncio.wait_for(run_docker.run_docker_with_outcome(claim.entry), 15)
    assert not outcome.success and "exit 17" in outcome.error
    assert (tmp_path / "starts").read_text().splitlines() == ["started"]
    assert any(command[:3] == ("docker", "start", "--attach") for command in commands)
    assert any(command[:2] == ("docker", "run") for command in commands) is late_creation
    observed = actual_inspect(claim.name)
    assert observed["State"]["Status"] == "exited" and observed["State"]["ExitCode"] == 17
    assert claim.api.tasks.fail_attempt.await_args.args[0]._execution_start_id == claim.entry._execution_start_id
    claim.api.tasks.termination_confirmed.assert_awaited_once()
    stopped = claim.api.tasks.termination_confirmed.await_args.args[0]
    assert claim.api.tasks.execution(stopped) == planned_execution


@pytest.mark.asyncio
async def test_cli_death_keeps_live_container_and_terminalizes_its_original_attempt(docker_claim, monkeypatch, tmp_path):
    from simstack.core import run_docker
    claim = docker_claim
    command = _docker_python_command(claim, tmp_path, action="run")
    cli = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "starts").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert (tmp_path / "starts").exists()
        cli.kill()
        await asyncio.to_thread(cli.communicate)
        canonical = claim.entry.model_copy(deep=True)
        canonical._execution_start_id = None
        claim.api.tasks.reconcile_attempts.return_value = {"claims": [{"state": "claimed", "task": canonical}]}
        inspect = run_docker.inspect_docker_execution
        with monkeypatch.context() as patch:
            patch.setattr(run_docker, "inspect_docker_execution", lambda _: (_ for _ in ()).throw(RuntimeError("Daemon query unavailable")))
            assert await recover_launches(claim.api) == []
        assert await recover_launches(claim.api) == []
        assert list(pending(claim.api)) and inspect(claim.name)["State"]["Status"] == "running"
        claim.api.tasks.fail_attempt.assert_not_awaited()
        canonical.error = "Original child bootstrap cause"
        (tmp_path / "release").touch()
        deadline = time.monotonic() + 10
        while inspect(claim.name)["State"]["Status"] != "exited" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert await recover_launches(claim.api) == []
        assert not list(pending(claim.api))
        failed = claim.api.tasks.fail_attempt.await_args
        assert failed.args[0]._execution_start_id == claim.entry._execution_start_id
        assert failed.kwargs["exit_code"] == 17
        assert canonical.error == "Original child bootstrap cause" and canonical.status == TaskStatus.FAILED
        assert (tmp_path / "starts").read_text().splitlines() == ["started"]
    finally:
        if cli.poll() is None:
            cli.kill()
        await asyncio.to_thread(cli.communicate)


@pytest.mark.asyncio
async def test_stale_task_cid_cannot_stop_a_competing_container(docker_claim, tmp_path):
    from simstack.core.run_docker import docker_execution_name, inspect_docker_execution
    from simstack.core.services.task_termination import stop_task_execution
    claim = docker_claim
    competing = claim.entry.model_copy(deep=True)
    competing._execution_generation = "new-generation"
    competing._execution_start_id = "new-start"
    new_name = docker_execution_name(claim.api, claim.api.tasks.execution(competing))
    claim.owned.append(new_name)
    for name, marker in ((claim.name, "old-start"), (new_name, "new-start")):
        subprocess.run(_docker_python_command(claim, tmp_path, name=name, marker=marker), check=True,
            capture_output=True, timeout=15)
        subprocess.run(["docker", "start", name], check=True, capture_output=True, timeout=15)
    claim.cidfile.write_text(inspect_docker_execution(new_name)["Id"])
    await stop_task_execution(claim.entry, server=claim.api, runner_pid=os.getpid(), workdir=tmp_path)
    assert inspect_docker_execution(claim.name)["State"]["Status"] == "exited"
    assert inspect_docker_execution(new_name)["State"]["Status"] == "running"


@pytest.mark.asyncio
async def test_sdk_cached_invocation_recovers_its_lost_local_publication(monkeypatch, tmp_path):
    from simstack.core.node import Node
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    api = api_client()
    api.requests.authorization = "user"
    input_publication = str(uuid4())
    api.values.publish_task_inputs = AsyncMock(return_value=([], input_publication))
    saved = []
    async def commit_then_lose(payload, *, request_id):
        assert payload["input_publication"] == input_publication
        entry = task_entry(ObjectId(payload["requested_task_id"]))
        entry._execution_local = True
        saved.append({"disposition": "launch_inline", "task": entry,
            "recompute_artifacts": False, "local_execution_token": "root-specific-token"})
        raise OSError("publication response lost")
    api.tasks.submit_root = AsyncMock(side_effect=commit_then_lose)
    mapping = SimpleNamespace(function_mapping="test.probe", repository_revision=None)
    active = SimpleNamespace(server=api, in_docker=False, current_node_name=None,
        config=SimpleNamespace(resource="local", project_root=tmp_path),
        node_mappings=SimpleNamespace(get_by_name=lambda name: mapping))
    monkeypatch.setattr("simstack.core.node.context", active)
    def probe(**kwargs):
        return True
    first = Node(func=probe, is_async=False, parameters=Parameters(resource="self"))
    with pytest.raises(OSError, match="response lost"):
        await first.get_node_registry()
    original_request = api.tasks.submit_root.await_args.kwargs["request_id"]
    api.tasks.get_request.return_value = {"state": "applied", "result": saved[0]}
    api.tasks.get_task.return_value = saved[0]["task"]
    api.tasks.reuse_invocation.side_effect = None
    api.tasks.reuse_invocation.return_value = {**saved[0], "disposition": "await_existing"}
    repeated = Node(func=probe, is_async=False, parameters=Parameters(resource="self"))
    await repeated.get_node_registry()
    assert repeated._owns_inline_nested_execution
    assert repeated._execution_server.requests.token == "root-specific-token"
    assert repeated.registry_entry.id == saved[0]["task"].id
    assert reserve_launch(api, repeated.id) is None
    api.tasks.submit_root.assert_awaited_once()
    assert api.values.publish_task_inputs.await_count == 2
    assert all(call.args == ([],) and call.kwargs == {"repository_revision": None}
               for call in api.values.publish_task_inputs.await_args_list)
    api.tasks.get_request.assert_awaited_once_with(original_request)
    repeated.run_node_as_process = AsyncMock(return_value=True)
    assert await repeated.run_somewhere()
    repeated.run_node_as_process.assert_awaited_once()
    reservation = reserve_launch(api, repeated.id)
    assert reservation is not None
    reservation.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Fork-based interprocess crash barrier")
async def test_same_actor_acquisition_lease_preserves_lost_reply_across_process_race(monkeypatch, tmp_path):
    import multiprocessing
    from simstack.runner_runtime import launch_claims as claims
    from simstack.server_api.http_transport import RunnerAPIError
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    multiprocessing_context = multiprocessing.get_context("fork")
    read_intent, publish_intent = multiprocessing_context.Event(), multiprocessing_context.Event()
    def first_runner():
        write = claims._write
        def pause_before_write(path, value):
            if path.name.startswith("work-acquisition-"):
                read_intent.set()
                if not publish_intent.wait(10):
                    raise RuntimeError("barrier timed out")
            write(path, value)
        claims._write = pause_before_write
        api = api_client()
        async def lose_reply(capacity, request_id):
            (tmp_path / "committed-request").write_text(request_id)
            raise RunnerAPIError("claim committed but reply lost")
        api.tasks.acquire_work.side_effect = lose_reply
        try:
            asyncio.run(claims.acquire_work(api))
        except RunnerAPIError:
            pass
    process = multiprocessing_context.Process(target=first_runner)
    process.start()
    try:
        assert await asyncio.to_thread(read_intent.wait, 5)
        second = api_client()
        assert await asyncio.wait_for(claims.acquire_work(second), 1) == []
        second.tasks.acquire_work.assert_not_awaited()
        publish_intent.set()
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode == 0
        committed_request = (tmp_path / "committed-request").read_text()
        entry = task_entry()
        second.tasks.acquire_work.return_value = [entry]
        assert await claims.acquire_work(second) == [entry]
        assert second.tasks.acquire_work.await_args.kwargs["request_id"] == committed_request
        assert len(list(claims.pending(second))) == 1
        assert not list(claims._directory(second.requests.server_url).glob("work-acquisition-*.json"))
    finally:
        publish_intent.set()
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 5)
