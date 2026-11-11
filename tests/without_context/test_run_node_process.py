import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import simstack.core.run_node as run_node_module
from simstack.core.definitions import TaskStatus
from simstack.core.node import Node
from simstack.core.run_node import run_node_from_id
from simstack.core.run_docker import CONTAINER_WORKDIR, DockerRunResult
from simstack.core.run_node_protocol import (
    RunNodeResult,
    encode_run_node_result,
    parse_run_node_result,
)
from simstack.core.services.node_execution_service import NodeExecutionOutcome
from simstack.models.parameters import Parameters
from simstack.server_api.workflow_tasks import WorkflowTasksAPI


class _Process:
    def __init__(self, returncode: int, stdout: str, stderr: str = "", *, streams=None) -> None:
        self.returncode = returncode
        self.pid = 23456
        if streams is not None:
            streams["stdout"].write(stdout.encode())
            streams["stderr"].write(stderr.encode())

    async def communicate(self):
        return None, None


class _DomainClient:
    def __init__(self, entry) -> None:
        self.entry = entry
        entry._execution_claim_id = getattr(entry, "_execution_claim_id", "test-claim")
        entry._execution_generation = getattr(entry, "_execution_generation", None)
        self.saved = []
        self.observed = []

    async def get_task(self, task_id):
        return self.entry

    async def get_outcome(self, entry):
        return self.entry

    def execution(self, entry):
        return WorkflowTasksAPI.execution(entry)

    async def reconcile_attempts(self, *, claims, outcomes, request_id=None):
        self.observed.extend(outcomes)
        return {"claims": [], "outcomes": []}

    async def fail_attempt(self, entry, error, **kwargs):
        self.saved.append((entry, error, kwargs))
        if self.entry.status in {TaskStatus.RETRIEVED, TaskStatus.RUNNING}:
            self.entry.status = TaskStatus.FAILED
            self.entry.error = self.entry.error or error
            self.entry.return_kind = "exception"
            return self.entry
        return None

    async def record_process(self, entry, pid, *, host_owner=False):
        self.entry.process_id = pid
        if host_owner:
            self.entry._execution_owner = {"execution": self.execution(entry), "process_id": pid}
        return self.entry


@pytest.fixture(autouse=True)
def isolated_runtime_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    # Journal recovery has its own filesystem/process tests. These protocol
    # unit tests deliberately use readable non-ObjectId task labels.
    monkeypatch.setattr("simstack.runner_runtime.launch_claims.record_spawn", lambda *args: None)



def _process_node(entry, parameters: Parameters | None = None) -> Node:
    entry.repository_revision = None
    execution_node = Node.__new__(Node)
    execution_node.name = "process_probe"
    if not hasattr(entry, "_execution_claim_id"):
        entry._execution_claim_id = "test-claim"
        entry._execution_generation = None
    execution_node.registry_entry = entry
    execution_node.parameters = parameters or Parameters(resource="self")
    return execution_node


async def run_claimed_from_id(entry, resource, **kwargs):
    from simstack.runner_runtime.launch_claims import task_execution
    with task_execution(entry):
        return await run_node_from_id(str(entry.id), resource, **kwargs)


def test_run_node_protocol_round_trip_and_rejects_invalid_types():
    result = RunNodeResult(False, "exception", "broken import")
    assert parse_run_node_result(encode_run_node_result(result)) == result
    assert (
        parse_run_node_result(
            'SIMSTACK_RUN_NODE_RESULT={"success":"false","return_kind":"bool"}'
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode,expected_exit,expected_signal", [(17, 17, None), (-9, None, 9)])
@pytest.mark.parametrize("unavailable", [False, True])
async def test_inline_early_failure_preserves_exit_signal_in_command_and_journal(
    monkeypatch, tmp_path, returncode, expected_exit, expected_signal, unavailable,
):
    from simstack.runner_runtime.pending_execution_failures import pending_failures
    from simstack.server_api.http_transport import RunnerAPIError
    entry = SimpleNamespace(id="early-child", status=TaskStatus.RETRIEVED, error=None, results_references=[])
    api = _DomainClient(entry)
    if unavailable:
        api.fail_attempt = AsyncMock(side_effect=RunnerAPIError("offline", status_code=503))
        api.reconcile_attempts = AsyncMock(side_effect=RunnerAPIError("offline", status_code=503))
    server = SimpleNamespace(tasks=api, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=server, in_docker=False,
        config=SimpleNamespace(resource="local", project_root=tmp_path)))
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", AsyncMock(return_value=_Process(returncode, "")))
    with pytest.raises(RuntimeError, match=f"exited with code {returncode}"):
        await _process_node(entry).run_node_as_process()
    payload = api.fail_attempt.await_args.kwargs if unavailable else api.saved[-1][2]
    assert payload["exit_code"] == expected_exit and payload["signal"] == expected_signal
    if unavailable:
        records = list(pending_failures())
        assert len(records) == 1
        assert records[0][1]["exit_code"] == expected_exit
        assert records[0][1]["signal"] == expected_signal
        assert records[0][1]["actor"]["account_id"] == "test-account"


@pytest.mark.asyncio
async def test_process_for_self_uses_actual_resource_and_preserves_true(
    monkeypatch, tmp_path
):
    entry = SimpleNamespace(
        id="task-one",
        status=TaskStatus.COMPLETED,
        error=None,
        results_references=[],
    )
    database = _DomainClient(entry)
    captured = []
    host_python_path = "/host-only/simstack/src"
    image_python_path = "/image/site-packages"
    monkeypatch.setenv("PYTHONPATH", image_python_path)
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://server.example/api")
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "runner-test-token")
    monkeypatch.setenv("SIMSTACK_DB_CONNECTION_STRING", "must-not-reach-child")

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _Process(0, encode_run_node_result(RunNodeResult(True, "bool")), streams=kwargs)

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=True,
            config=SimpleNamespace(
                resource="cluster-a",
                project_root=tmp_path,
                python_paths=[host_python_path],
                connection_string="mongodb://user:password@db:27017/",
                db_name="child-database",
            ),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = await _process_node(entry).run_node_as_process()

    assert result is True
    command = captured[0][0]
    assert command[command.index("--resource") + 1] == "cluster-a"
    assert "--in-docker" in command
    child_env = captured[0][1]["env"]
    assert "SIMSTACK_DB_CONNECTION_STRING" not in child_env
    assert "SIMSTACK_DB_DATABASE" not in child_env
    assert child_env["SIMSTACK_SERVER_URL"] == "https://server.example/api"
    assert child_env["SIMSTACK_RUNNER_TOKEN"] == "runner-test-token"
    assert captured[0][1]["cwd"] == str(tmp_path)
    assert child_env["PYTHONPATH"].split(os.pathsep) == [
        str(tmp_path),
        image_python_path,
    ]
    assert host_python_path not in child_env["PYTHONPATH"]


@pytest.mark.asyncio
async def test_process_preserves_successful_none_return(monkeypatch, tmp_path):
    entry = SimpleNamespace(
        id="task-none",
        status=TaskStatus.COMPLETED,
        error=None,
        results_references=[],
    )
    database = _DomainClient(entry)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return _Process(0, encode_run_node_result(RunNodeResult(True, "none")), streams=kwargs)

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=False,
            config=SimpleNamespace(
                resource="local",
                project_root=tmp_path,
                connection_string=None,
            ),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    assert await _process_node(entry).run_node_as_process() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("return_kind", ["model", "multiple"])
async def test_process_reloads_persisted_model_results(
    monkeypatch, tmp_path, return_kind
):
    entry = SimpleNamespace(
        id=f"task-{return_kind}",
        status=TaskStatus.COMPLETED,
        error=None,
        results_references=[object()],
    )
    database = _DomainClient(entry)
    sentinel = SimpleNamespace(kind=return_kind)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return _Process(
            0, encode_run_node_result(RunNodeResult(True, return_kind)), streams=kwargs
        )

    async def fake_load_results(self, return_kind=None):
        assert return_kind == return_kind_from_child
        return sentinel

    return_kind_from_child = return_kind

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=False,
            config=SimpleNamespace(
                resource="local",
                project_root=tmp_path,
                connection_string=None,
            ),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(Node, "load_results", fake_load_results)

    assert await _process_node(entry).run_node_as_process() is sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize(("return_kind", "failure"), [
    ("bool", 503), ("model", 503), ("multiple", 503), ("bool", None),
    ("bool", 401), ("bool", 403), ("bool", "decode"),
])
async def test_successful_child_accepts_only_transient_read_failure_for_bool(monkeypatch, tmp_path, return_kind, failure):
    from simstack.server_api.http_transport import RunnerAPIError

    entry = SimpleNamespace(id="completed-child", status=TaskStatus.RUNNING, error=None, results_references=[])
    api = _DomainClient(entry)
    error = (ValueError("invalid outcome response") if failure == "decode" else
        RunnerAPIError("outcome read unavailable", status_code=failure))
    api.get_outcome = AsyncMock(side_effect=error)
    api.fail_attempt = AsyncMock()
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(in_docker=False,
        config=SimpleNamespace(resource="local", project_root=tmp_path, python_paths=[]),
        server=SimpleNamespace(tasks=api, requests=SimpleNamespace(server_url="https://server.example/api",
            authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))))
    async def spawn(*args, **kwargs):
        return _Process(0, encode_run_node_result(RunNodeResult(True, return_kind)), streams=kwargs)
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", spawn)
    node = _process_node(entry)
    if return_kind == "bool" and failure in {503, None}:
        assert await node.run_node_as_process() is True
        assert node.status == TaskStatus.COMPLETED and entry.return_kind == "bool"
    else:
        with pytest.raises(type(error), match=str(error)):
            await node.run_node_as_process()
        assert entry.status == TaskStatus.RUNNING
    api.fail_attempt.assert_not_awaited()
    assert node.registry_entry is entry


@pytest.mark.asyncio
async def test_process_failure_is_nonzero_bounded_and_redacts_child_output(
    monkeypatch, tmp_path
):
    secret = "mongodb://alice:super-secret@db.internal:27017/simstack"
    entry = SimpleNamespace(
        id="task-failure",
        status=TaskStatus.FAILED,
        error=f"database failed at {secret}",
        results_references=[],
    )
    database = _DomainClient(entry)
    protocol = encode_run_node_result(
        RunNodeResult(False, "exception", f"child failed at {secret}")
    )

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return _Process(
            1,
            f"{'x' * 5000}{secret}\n{protocol}",
            f"{'y' * 5000}{secret}", streams=kwargs,
        )

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=False,
            config=SimpleNamespace(
                resource="local",
                project_root=tmp_path,
                connection_string=secret,
            ),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError) as caught:
        await _process_node(entry).run_node_as_process()

    assert secret not in str(caught.value)
    assert "alice" not in str(caught.value)
    assert "super-secret" not in str(caught.value)
    assert entry.error == f"database failed at {secret}"
    assert entry.status == TaskStatus.FAILED
    assert database.saved == []  # A terminal child keeps its primary failure.
    assert database.observed[0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_process_spawn_failure_is_saved_without_database_credentials(
    monkeypatch, tmp_path
):
    secret = "mongodb://alice:super-secret@db.internal:27017/simstack"
    entry = SimpleNamespace(
        id="task-spawn",
        status=TaskStatus.RETRIEVED,
        error=None,
        results_references=[],
    )
    database = _DomainClient(entry)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        raise OSError(f"cannot spawn with {secret}")

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=False,
            config=SimpleNamespace(
                resource="local",
                project_root=tmp_path,
                connection_string=secret,
            ),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError) as caught:
        await _process_node(entry).run_node_as_process()

    assert secret not in str(caught.value)
    assert "super-secret" not in entry.error
    assert entry.status == TaskStatus.FAILED
    assert database.saved[-1][0] is entry


@pytest.mark.asyncio
async def test_context_initialization_error_uses_environment_secret_for_redaction(
    monkeypatch, caplog
):
    secret = "mongodb://env-user:env-password@db.internal:27017/simstack"

    async def fail_initialize(**kwargs):
        raise RuntimeError(f"cannot connect to {secret}")

    monkeypatch.setenv("SIMSTACK_DB_CONNECTION_STRING", secret)
    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(initialize=fail_initialize),
    )

    result = await run_node_from_id("task-id", "local")

    assert result.success is False
    assert secret not in (result.error or "")
    assert "env-user" not in caplog.text
    assert "env-password" not in caplog.text


@pytest.mark.asyncio
async def test_context_system_exit_still_emits_sanitized_failure(monkeypatch):
    secret = "mongodb://env-user:env-password@db.internal:27017/simstack"

    async def fail_initialize(**kwargs):
        raise SystemExit(f"cannot connect to {secret}")

    monkeypatch.setenv("SIMSTACK_DB_CONNECTION_STRING", secret)
    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(initialize=fail_initialize),
    )

    result = await run_node_from_id("task-id", "local")

    assert result.success is False
    assert secret not in (result.error or "")
    assert "env-user" not in (result.error or "")


@pytest.mark.asyncio
async def test_runtime_system_exit_is_persisted_as_nonempty_failure(monkeypatch):
    entry = SimpleNamespace(
        id="task-id",
        parameters=Parameters(in_docker=False),
        status=TaskStatus.RETRIEVED,
        error=None,
        return_kind=None,
    )
    database = _DomainClient(entry)

    async def initialize(**kwargs):
        return None

    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            initialized=True,
            config=SimpleNamespace(connection_string=None),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.run_node_from_registry_with_outcome",
        AsyncMock(side_effect=SystemExit()),
    )

    result = await run_claimed_from_id(entry, "local")

    assert result == RunNodeResult(False, "exception", "SystemExit")
    assert entry.status == TaskStatus.FAILED
    assert entry.error == "SystemExit"
    assert entry.return_kind == "exception"
    assert database.saved[-1][0] is entry


@pytest.mark.asyncio
async def test_false_return_kind_is_preserved_as_failed_child_outcome(monkeypatch):
    entry = SimpleNamespace(
        id="task-id",
        parameters=Parameters(in_docker=False),
        status=TaskStatus.FAILED,
        error=None,
    )
    database = _DomainClient(entry)

    async def initialize(**kwargs):
        return None

    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            initialized=True,
            config=SimpleNamespace(connection_string=None),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.run_node_from_registry_with_outcome",
        AsyncMock(return_value=NodeExecutionOutcome(False, "bool")),
    )

    result = await run_claimed_from_id(entry, "local")

    assert result == RunNodeResult(False, "bool")


@pytest.mark.asyncio
async def test_terminating_task_is_not_started(monkeypatch):
    entry = SimpleNamespace(
        id="task-id",
        parameters=Parameters(in_docker=True),
        status=TaskStatus.TERMINATING,
        error=None,
    )
    database = _DomainClient(entry)

    async def initialize(**kwargs):
        return None

    started = AsyncMock(side_effect=AssertionError("terminating task was started"))
    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            initialized=True,
            config=SimpleNamespace(connection_string=None),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.run_node_from_registry_with_outcome",
        started,
    )
    monkeypatch.setattr("simstack.core.run_node.run_docker_with_outcome", started)

    result = await run_claimed_from_id(entry, "local")

    assert result == RunNodeResult(False, "none", "Terminated by user")
    started.assert_not_called()
    assert not hasattr(entry, "process_id") or entry.process_id is None


@pytest.mark.asyncio
async def test_in_container_context_uses_shared_non_root_workdir(monkeypatch):
    entry = SimpleNamespace(
        id="task-id",
        parameters=Parameters(in_docker=True),
        status=TaskStatus.RETRIEVED,
        error=None,
    )
    database = _DomainClient(entry)
    init_kwargs = {}

    async def initialize(**kwargs):
        init_kwargs.update(kwargs)

    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            initialized=True,
            config=SimpleNamespace(connection_string=None),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node._run_local_controller",
        AsyncMock(return_value=RunNodeResult(True, "model")),
    )

    result = await run_claimed_from_id(entry, "local", in_docker=True)

    assert result == RunNodeResult(True, "model")
    assert init_kwargs["workdir"] == CONTAINER_WORKDIR
    assert CONTAINER_WORKDIR == "/mnt"
    assert not CONTAINER_WORKDIR.startswith("/tmp")
    assert not CONTAINER_WORKDIR.startswith("/root/")


@pytest.mark.asyncio
async def test_docker_child_return_kind_is_forwarded(monkeypatch):
    entry = SimpleNamespace(
        id="task-id",
        parameters=Parameters(in_docker=True),
        status=TaskStatus.COMPLETED,
        error=None,
        results_references=[object(), object()],
    )
    database = _DomainClient(entry)
    database.get_outcome = AsyncMock(side_effect=AssertionError("Docker outcome was already verified"))

    async def initialize(**kwargs):
        return None

    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            initialized=True,
            config=SimpleNamespace(connection_string=None),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.run_docker_with_outcome",
        AsyncMock(return_value=DockerRunResult(True, "multiple")),
    )

    assert await run_claimed_from_id(entry, "local") == RunNodeResult(
        True, "multiple"
    )
    database.get_outcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_wait_preserves_true_and_raises_sanitized_failure(
    monkeypatch
):
    completed = SimpleNamespace(
        id="remote-task",
        status=TaskStatus.COMPLETED,
        error=None,
        results_references=[],
    )
    database = _DomainClient(completed)
    execution_node = _process_node(completed, Parameters(resource="remote"))
    execution_node.name = "remote_probe"
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            config=SimpleNamespace(connection_string=None),
            server=SimpleNamespace(tasks=database, requests=SimpleNamespace(
                server_url="https://server.example/api", authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        ),
    )

    assert await execution_node._wait_for_remote_completion() is True

    secret = "mongodb://remote-user:remote-password@db.internal/simstack"
    failed = SimpleNamespace(
        id="remote-task",
        status=TaskStatus.FAILED,
        error=f"child failed at {secret}",
        results_references=[],
    )
    database.entry = failed

    with pytest.raises(RuntimeError) as caught:
        await execution_node._wait_for_remote_completion()

    assert secret not in str(caught.value)
    assert "remote-user" not in str(caught.value)
    assert execution_node.registry_entry is failed


def test_run_node_cli_emits_protocol_and_exits_nonzero_on_import_failure(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        run_node_module,
        "run_node_from_id",
        AsyncMock(
            return_value=RunNodeResult(False, "exception", "node import failed")
        ),
    )
    monkeypatch.setattr(
        run_node_module.sys,
        "argv",
        ["run_node", "--node-id", "task-id", "--resource", "local"],
    )

    with pytest.raises(SystemExit) as caught:
        run_node_module.run_node_main()

    assert caught.value.code == 1
    protocol = parse_run_node_result(capsys.readouterr().out)
    assert protocol == RunNodeResult(False, "exception", "node import failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_status", [TaskStatus.FAILED, TaskStatus.COMPLETED, TaskStatus.TERMINATING, TaskStatus.RUNNING])
async def test_stored_child_error_wins_over_different_protocol_error_and_fallback_failure(
    monkeypatch, tmp_path, caplog, stored_status,
):
    from simstack.models.document import ObjectId
    from simstack.runner_runtime.launch_claims import task_execution, current_execution
    launch = SimpleNamespace(id=ObjectId(), status=TaskStatus.RETRIEVED, error=None,
        _execution_claim_id="original-claim", _execution_generation="original-generation")
    stored = SimpleNamespace(id=launch.id, status=stored_status,
        error="child stored diagnostic", _execution_claim_id="original-claim",
        _execution_generation="original-generation")
    captured = {}

    async def spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        return _Process(17, encode_run_node_result(
            RunNodeResult(False, "exception", "different protocol diagnostic")), streams=kwargs)

    async def fail_attempt(entry, error, **kwargs):
        assert entry is launch
        assert entry._execution_start_id == captured["SIMSTACK_EXECUTION_START"]
        assert entry._execution_claim_id == "original-claim"
        assert entry._execution_generation == "original-generation"
        raise RuntimeError("failure write unavailable")

    api = SimpleNamespace(get_outcome=AsyncMock(return_value=stored),
        fail_attempt=AsyncMock(side_effect=fail_attempt), reconcile_attempts=AsyncMock())
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=SimpleNamespace(tasks=api, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"})),
        in_docker=False, config=SimpleNamespace(resource="local", project_root=tmp_path)))
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", spawn)
    parent = SimpleNamespace(id=ObjectId(), _execution_claim_id="parent-claim")
    with task_execution(parent):
        parent_identity = dict(current_execution())
        with pytest.raises(RuntimeError, match="child stored diagnostic") as caught:
            await _process_node(launch).run_node_as_process()
        assert current_execution() == parent_identity
    assert "different protocol diagnostic" not in str(caught.value)
    assert "failure write unavailable" not in str(caught.value)
    assert stored.status == stored_status
    assert stored.error == "child stored diagnostic"
    if stored_status == TaskStatus.RUNNING:
        api.fail_attempt.assert_awaited_once()
        assert "failure write unavailable" in caplog.text
    else:
        api.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["task_id", "generation", "claim_id", "start_id", "missing_identity"])
async def test_stale_cli_launch_cannot_prepare_or_fail_a_competing_attempt(monkeypatch, changed):
    from simstack.runner_runtime.launch_claims import task_execution
    original = SimpleNamespace(id="task-id", _execution_generation="original-generation",
        _execution_claim_id="original-claim", _execution_start_id="original-start")
    current = SimpleNamespace(**vars(original))
    if changed == "task_id":
        current.id = "competing-task"
    elif changed != "missing_identity":
        setattr(current, "_execution_" + changed, "competing-" + changed)
    tasks = SimpleNamespace(get_task=AsyncMock(return_value=current), fail_attempt=AsyncMock())
    prepare = AsyncMock(side_effect=AssertionError("prepared competing attempt"))
    monkeypatch.setattr("simstack.core.run_node.context", SimpleNamespace(
        initialize=AsyncMock(), server=SimpleNamespace(tasks=tasks)))
    monkeypatch.setattr("simstack.runner_runtime.execution_environment.prepare_task", prepare)
    if changed == "missing_identity":
        for key in ("TASK", "GENERATION", "CLAIM", "START"):
            monkeypatch.delenv("SIMSTACK_EXECUTION_" + key, raising=False)
        result = await run_node_from_id("task-id", "local")
        tasks.get_task.assert_not_awaited()
    else:
        with task_execution(original):
            result = await run_node_from_id("task-id", "local")
    assert not result.success
    prepare.assert_not_awaited()
    tasks.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("outage", "ack_outage"), [(False, False), (True, False), (False, True)])
async def test_controller_waits_for_owned_worker_before_confirming_cancellation(monkeypatch, tmp_path, outage, ack_outage):
    from simstack.core.services import task_termination
    from simstack.server_api.http_transport import RunnerAPIError

    entry = SimpleNamespace(id="owned-worker", status=TaskStatus.RUNNING, error=None,
        _execution_generation="generation", _execution_claim_id="claim", _execution_start_id="start")
    stopped = asyncio.Event()
    async def communicate():
        await stopped.wait()
        return b"worker output", b""
    process = SimpleNamespace(pid=12345, returncode=None, communicate=communicate, wait=AsyncMock())
    reads = 0
    async def get_outcome(original):
        nonlocal reads
        assert original is entry
        reads += 1
        if outage and reads == 1:
            raise RunnerAPIError("temporary cancellation read outage", status_code=503)
        return SimpleNamespace(**{**vars(entry), "status": TaskStatus.TERMINATING, "error": "last consumer stopped"})
    async def stop_owned(current, **kwargs):
        assert reads == (2 if outage else 1)
        assert kwargs["local_process"] is process and kwargs["runner_pid"] == 99999
        process.returncode = -9
        await process.wait()
        stopped.set()
    acknowledgements = 0
    async def confirmed(original, error):
        nonlocal acknowledgements
        acknowledgements += 1
        assert original is entry and error == "last consumer stopped"
        process.wait.assert_awaited_once()
        assert process.returncode == -9
        if ack_outage and acknowledgements == 1:
            raise RunnerAPIError("temporary termination acknowledgement outage", status_code=503)
    tasks = SimpleNamespace(execution=WorkflowTasksAPI.execution,
        get_outcome=AsyncMock(side_effect=get_outcome), termination_confirmed=AsyncMock(side_effect=confirmed))
    server = SimpleNamespace(tasks=tasks)
    stop = AsyncMock(side_effect=stop_owned)
    monkeypatch.setattr(task_termination, "stop_task_execution", stop)
    actual_wait = asyncio.wait
    async def poll(tasks, *, timeout):
        return await actual_wait(tasks, timeout=0)
    monkeypatch.setattr(task_termination.asyncio, "wait", poll)
    assert await task_termination.monitor_task_process(entry, process, server=server,
        workdir=tmp_path, runner_pid=99999) == (b"worker output", b"")
    stop.assert_awaited_once()
    assert tasks.termination_confirmed.await_count == (2 if ack_outage else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["task_id", "generation", "claim_id", "start_id"])
async def test_controller_never_uses_competing_attempt_to_stop_or_acknowledge(monkeypatch, tmp_path, changed):
    from simstack.core.services import task_termination

    entry = SimpleNamespace(id="original-worker", status=TaskStatus.RUNNING, error=None,
        _execution_generation="generation", _execution_claim_id="claim", _execution_start_id="start")
    current = SimpleNamespace(**{**vars(entry), "status": TaskStatus.TERMINATING})
    setattr(current, "id" if changed == "task_id" else "_execution_" + changed, "competing-" + changed)
    finished = asyncio.Event()
    async def communicate():
        await finished.wait()
        return b"original worker finished", b""
    process = SimpleNamespace(pid=12345, returncode=None, communicate=communicate)
    async def get_outcome(original):
        assert original is entry
        process.returncode = 0
        finished.set()
        await asyncio.sleep(0)
        return current
    tasks = SimpleNamespace(execution=WorkflowTasksAPI.execution,
        get_outcome=AsyncMock(side_effect=get_outcome), termination_confirmed=AsyncMock())
    stop = AsyncMock(side_effect=AssertionError("a competing attempt cannot grant stop authority"))
    monkeypatch.setattr(task_termination, "stop_task_execution", stop)
    actual_wait = asyncio.wait
    async def poll(tasks, *, timeout):
        return await actual_wait(tasks, timeout=0)
    monkeypatch.setattr(task_termination.asyncio, "wait", poll)
    assert await task_termination.monitor_task_process(entry, process, server=SimpleNamespace(tasks=tasks),
        workdir=tmp_path, runner_pid=99999) == (b"original worker finished", b"")
    stop.assert_not_awaited()
    tasks.termination_confirmed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("local", "local_worker", "in_docker", "docker_job"), [
    (True, False, False, False), (True, True, False, False),
    (True, False, True, False), (False, False, False, False), (True, False, False, True),
])
async def test_run_node_supervises_only_initial_native_local_launch(monkeypatch, tmp_path, local, local_worker, in_docker, docker_job):
    entry = SimpleNamespace(id="controller-dispatch", parameters=Parameters(in_docker=docker_job),
        status=TaskStatus.RETRIEVED, error=None, _execution_local=local)
    api = _DomainClient(entry)
    monkeypatch.setattr(run_node_module, "context", SimpleNamespace(initialize=AsyncMock(), in_docker=in_docker,
        config=SimpleNamespace(project_root=tmp_path, workdir=tmp_path), server=SimpleNamespace(tasks=api)))
    controller = AsyncMock(return_value=RunNodeResult(True, "bool"))
    worker = AsyncMock(return_value=NodeExecutionOutcome(True, "bool"))
    docker = AsyncMock(return_value=DockerRunResult(True, "bool"))
    monkeypatch.setattr(run_node_module, "_run_local_controller", controller)
    monkeypatch.setattr(run_node_module, "run_node_from_registry_with_outcome", worker)
    monkeypatch.setattr(run_node_module, "run_docker_with_outcome", docker)
    controller_pid = os.getppid() if local_worker else None
    result = await run_claimed_from_id(entry, "local", project_root=str(tmp_path),
        in_docker=in_docker, local_worker=controller_pid)
    assert result == RunNodeResult(True, "bool")
    if not local_worker and (in_docker or local and not docker_job):
        controller.assert_awaited_once()
        worker.assert_not_awaited()
        docker.assert_not_awaited()
    elif docker_job and not in_docker:
        docker.assert_awaited_once()
        worker.assert_not_awaited()
        controller.assert_not_awaited()
    else:
        worker.assert_awaited_once()
        assert worker.await_args.kwargs.get("process_id") == (os.getppid() if local_worker else None)
        controller.assert_not_awaited()
        docker.assert_not_awaited()


@pytest.fixture
def native_controller(monkeypatch, tmp_path):
    entry = SimpleNamespace(id="controller-child", repository_revision=None,
        parameters=Parameters(), status=TaskStatus.RUNNING, error=None,
        _execution_local=True, _execution_resource="local", _execution_generation="generation",
        _execution_claim_id="claim", _execution_start_id="start")
    tasks = SimpleNamespace(execution=WorkflowTasksAPI.execution,
        get_outcome=AsyncMock(), termination_confirmed=AsyncMock(), fail_attempt=AsyncMock(),
        reconcile_attempts=AsyncMock())
    server = SimpleNamespace(tasks=tasks, requests=SimpleNamespace(server_url="https://server.example/api",
        resource="local", authorization="local", token="scoped-child-token", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr(run_node_module, "context", SimpleNamespace(server=server, in_docker=False,
        config=SimpleNamespace(resource="local", project_root=tmp_path, workdir=tmp_path)))
    scenario = SimpleNamespace(entry=entry, server=server, tasks=tasks, command=None, process=None,
        result=RunNodeResult(True, "bool"), returncode=0, root=tmp_path)
    async def spawn(*command, **kwargs):
        scenario.command = command
        scenario.process = _Process(scenario.returncode,
            encode_run_node_result(scenario.result) if scenario.result is not None else "", streams=kwargs)
        scenario.process.communicate = AsyncMock(wraps=scenario.process.communicate)
        return scenario.process
    monkeypatch.setattr(run_node_module.asyncio, "create_subprocess_exec", spawn)
    return scenario


@pytest.mark.asyncio
@pytest.mark.parametrize("return_kind", ["bool", "model"])
async def test_controller_preserves_worker_success_without_an_extra_outcome_read(native_controller, return_kind):
    from simstack.server_api.http_transport import RunnerAPIError

    scenario = native_controller
    scenario.result = RunNodeResult(True, return_kind)
    scenario.tasks.get_outcome.side_effect = RunnerAPIError("outcome read unavailable", status_code=503)
    assert await run_node_module._run_local_controller(scenario.entry, "local", scenario.root) == scenario.result
    scenario.process.communicate.assert_awaited_once()
    scenario.tasks.get_outcome.assert_not_awaited()
    scenario.tasks.fail_attempt.assert_not_awaited()
    scenario.tasks.termination_confirmed.assert_not_awaited()
    assert scenario.command[-2:] == ("--local-worker", str(os.getpid()))
    scenario.tasks.reconcile_attempts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [17, -9])
@pytest.mark.parametrize("protocol_survived", [False, True])
@pytest.mark.parametrize("delivery", ["accepted", "offline", "forbidden", "conflict", "no_journal", "both_unavailable_once", "journal_recovers"])
async def test_controller_preserves_completed_worker_exit_evidence(native_controller, monkeypatch, code, protocol_survived, delivery):
    from simstack.runner_runtime import pending_execution_failures as failures
    from simstack.server_api.http_transport import RunnerAPIError
    scenario = native_controller
    scenario.returncode = code
    if not protocol_survived:
        scenario.result = None
        async def completed(*args):
            if delivery not in {"no_journal", "both_unavailable_once", "journal_recovers"}:
                assert len(list(failures.pending_failures())) == 1, "exit must be durable before an outcome read"
            return SimpleNamespace(status=TaskStatus.COMPLETED, return_kind="bool")
        scenario.tasks.get_outcome.side_effect = completed
    if delivery in {"offline", "forbidden", "conflict"}:
        status = {"offline": 503, "forbidden": 403, "conflict": 409}[delivery]
        scenario.tasks.reconcile_attempts.side_effect = RunnerAPIError("delivery unavailable", status_code=status)
    if delivery in {"no_journal", "both_unavailable_once"}:
        monkeypatch.setattr(failures, "record_failure", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("read-only journal")))
    if delivery == "journal_recovers":
        real_record = failures.record_failure
        writes = 0
        def write(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise PermissionError("read-only journal")
            return real_record(*args, **kwargs)
        monkeypatch.setattr(failures, "record_failure", write)
        scenario.tasks.reconcile_attempts.side_effect = RunnerAPIError("offline", status_code=503)
    if delivery == "both_unavailable_once":
        scenario.tasks.reconcile_attempts.side_effect = [RunnerAPIError("offline", status_code=503), None]
        monkeypatch.setattr(run_node_module.asyncio, "sleep", AsyncMock())
    result = await run_node_module._run_local_controller(scenario.entry, "local", scenario.root)
    assert result == RunNodeResult(True, "bool")
    calls = scenario.tasks.reconcile_attempts.await_args_list
    assert len(calls) == (2 if delivery == "both_unavailable_once" else 1)
    assert len({call.kwargs["request_id"] for call in calls}) == 1
    outcome = calls[0].kwargs["outcomes"][0]
    assert outcome["execution"] == WorkflowTasksAPI.execution(scenario.entry)
    assert outcome["exit_code"] == (code if code >= 0 else None)
    assert outcome["signal"] == (-code if code < 0 else None)
    records = list(failures.pending_failures())
    assert len(records) == int(delivery in {"offline", "forbidden", "conflict", "journal_recovers"})
    if records:
        assert records[0][1]["request_id"] == outcome["request_id"]
        assert records[0][1]["execution"] == outcome["execution"]
    scenario.tasks.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_retains_exit_before_an_unavailable_outcome_read(native_controller, monkeypatch):
    from simstack.runner_runtime.pending_execution_failures import pending_failures
    from simstack.server_api.http_transport import RunnerAPIError
    scenario = native_controller
    scenario.returncode = 17
    scenario.result = None
    receipts = []
    async def read(*args):
        stored = list(pending_failures())
        assert len(stored) == 1 and stored[0][1]["exit_code"] == 17
        receipts.append(stored[0][1]["request_id"])
        if len(receipts) == 1:
            raise RunnerAPIError("outcome unavailable", status_code=503)
        return SimpleNamespace(status=TaskStatus.COMPLETED, return_kind="bool")
    scenario.tasks.get_outcome.side_effect = read
    monkeypatch.setattr(run_node_module.asyncio, "sleep", AsyncMock())
    assert await run_node_module._run_local_controller(scenario.entry, "local", scenario.root) == RunNodeResult(True, "bool")
    assert len(receipts) == 2 and len(set(receipts)) == 1
    assert not list(pending_failures())
    scenario.tasks.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_zero_exit_without_protocol_or_committed_outcome_is_failure(native_controller):
    scenario = native_controller
    scenario.result = None
    scenario.tasks.get_outcome.return_value = None
    scenario.tasks.fail_attempt.return_value = SimpleNamespace(error="missing scientific outcome", return_kind="exception")
    result = await run_node_module._run_local_controller(scenario.entry, "local", scenario.root)
    assert result == RunNodeResult(False, "exception", "missing scientific outcome")
    scenario.tasks.fail_attempt.assert_awaited_once()
    assert scenario.tasks.fail_attempt.await_args.kwargs["exit_code"] == 0
    scenario.tasks.reconcile_attempts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_survived", [False, True])
@pytest.mark.parametrize("ack_status", [403, 503])
@pytest.mark.parametrize("journal_recovers", [False, True])
async def test_completed_controller_retains_exit_before_uncertain_stop_ack(native_controller, monkeypatch,
        protocol_survived, ack_status, journal_recovers):
    from simstack.runner_runtime import pending_execution_failures as failures
    from simstack.server_api.http_transport import RunnerAPIError
    scenario = native_controller
    scenario.returncode = 17
    scenario.entry._execution_owner = {"process_id": os.getpid(),
        "execution": WorkflowTasksAPI.execution(scenario.entry)}
    if not protocol_survived:
        scenario.result = None
        scenario.tasks.get_outcome.return_value = SimpleNamespace(status=TaskStatus.COMPLETED, return_kind="bool")
    real_record = failures.record_failure
    writes = 0
    def write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if not journal_recovers or writes == 1:
            raise PermissionError("read-only journal")
        return real_record(*args, **kwargs)
    monkeypatch.setattr(failures, "record_failure", write)
    monkeypatch.setattr(run_node_module.asyncio, "sleep", AsyncMock())
    # A lost delivery reply is retried under the same immutable receipt.
    delivered = []
    async def reconcile(*args, **kwargs):
        delivered.append(kwargs)
        if len(delivered) == 1:
            raise RunnerAPIError("reconcile temporarily unavailable", status_code=503)
    scenario.tasks.reconcile_attempts.side_effect = reconcile
    acknowledgements = 0
    async def acknowledge(*args):
        nonlocal acknowledgements
        acknowledgements += 1
        assert delivered, "Physical exit reconciliation must precede a rejecting or retrying ACK"
        if not journal_recovers:
            assert len(delivered) >= 2, "The only in-memory receipt must reach Server first"
        else:
            assert list(failures.pending_failures()) or len(delivered) >= 2
        if acknowledgements == 1:
            raise RunnerAPIError("physical ACK rejected", status_code=ack_status)
    scenario.tasks.termination_confirmed.side_effect = acknowledge
    if protocol_survived and ack_status == 403:
        with pytest.raises(RunnerAPIError, match="physical ACK rejected"):
            await run_node_module._run_local_controller(scenario.entry, "local", scenario.root)
    else:
        result = await run_node_module._run_local_controller(scenario.entry, "local", scenario.root)
        assert result.success == (ack_status == 503)
    assert acknowledgements == (2 if ack_status == 503 else 1)
    assert all(call["outcomes"][0]["exit_code"] == 17 for call in delivered)
    assert len({call["request_id"] for call in delivered}) == 1
    scenario.tasks.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [-9, 0, 17])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, asyncio.CancelledError, SystemExit])
@pytest.mark.parametrize("delivery", ["accepted", "invalid_json", "both_unavailable_once"])
async def test_controller_retains_physical_exit_when_monitor_raises(native_controller, monkeypatch, code, error_type, delivery):
    import json
    from simstack.core.services import task_termination
    from simstack.runner_runtime import pending_execution_failures as failures
    scenario = native_controller
    scenario.returncode = code
    original_error = error_type("original monitor/cancellation cause")
    monkeypatch.setattr(task_termination, "monitor_task_process", AsyncMock(side_effect=original_error))
    cleaned = False
    async def cleanup(process):
        nonlocal cleaned
        assert process is scenario.process
        cleaned = True
    monkeypatch.setattr(task_termination, "stop_owned_process_group", cleanup)
    real_record = failures.record_failure
    def record(*args, **kwargs):
        assert cleaned, "A terminal outcome cannot precede verified physical cleanup"
        if delivery == "both_unavailable_once":
            raise PermissionError("journal inaccessible")
        return real_record(*args, **kwargs)
    monkeypatch.setattr(failures, "record_failure", record)
    if delivery == "invalid_json":
        scenario.tasks.reconcile_attempts.side_effect = json.JSONDecodeError("invalid reconcile response", "<html>", 0)
    elif delivery == "both_unavailable_once":
        scenario.tasks.reconcile_attempts.side_effect = [json.JSONDecodeError("invalid reconcile response", "<html>", 0), None]
        monkeypatch.setattr(run_node_module.asyncio, "sleep", AsyncMock())
    with pytest.raises(error_type) as caught:
        await run_node_module._run_local_controller(scenario.entry, "local", scenario.root)
    assert caught.value is original_error
    calls = scenario.tasks.reconcile_attempts.await_args_list
    assert len(calls) == (2 if delivery == "both_unavailable_once" else 1)
    assert len({call.kwargs["request_id"] for call in calls}) == 1
    receipt = calls[0].kwargs["outcomes"][0]
    assert receipt["error"] == "original monitor/cancellation cause"
    assert receipt["exit_code"] == (code if code >= 0 else None)
    assert receipt["signal"] == (-code if code < 0 else None)
    assert receipt["execution"] == WorkflowTasksAPI.execution(scenario.entry)
    assert len(list(failures.pending_failures())) == int(delivery == "invalid_json")
    scenario.tasks.get_outcome.assert_not_awaited()
    scenario.tasks.fail_attempt.assert_not_awaited()
    scenario.tasks.termination_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_confirms_already_exited_terminating_worker(native_controller):
    scenario = native_controller
    scenario.result = RunNodeResult(False, "none", "last consumer stopped")
    scenario.returncode = 1
    current = SimpleNamespace(**{**vars(scenario.entry), "status": TaskStatus.TERMINATING,
        "error": "last consumer stopped"})
    scenario.tasks.get_outcome.return_value = current
    async def confirmed(original, error):
        assert original is scenario.entry and error == "last consumer stopped"
        scenario.process.communicate.assert_awaited_once()
        assert scenario.process.returncode == 1
    scenario.tasks.termination_confirmed.side_effect = confirmed
    result = await run_node_module._run_local_controller(scenario.entry, "local", scenario.root)
    assert not result.success and result.error == "last consumer stopped"
    scenario.tasks.termination_confirmed.assert_awaited_once()
    scenario.tasks.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_keeps_controller_pid_in_both_start_acknowledgements(monkeypatch):
    from simstack.core.services import node_execution_service

    entry = SimpleNamespace(id="scientific-worker", status=TaskStatus.RETRIEVED, error=None,
        parameters=Parameters(), results_references=[])
    node = _process_node(entry)
    api = SimpleNamespace(start_attempt=AsyncMock(return_value=entry), get_outcome=AsyncMock(return_value=entry))
    active = SimpleNamespace(server=SimpleNamespace(tasks=api), in_docker=False)
    monkeypatch.setattr("simstack.core.node.context", active)
    monkeypatch.setattr(node_execution_service, "context", active)
    monkeypatch.setattr(node_execution_service, "node_from_task", AsyncMock(return_value=node))
    monkeypatch.setattr("simstack.runner_runtime.launch_claims.acknowledge_start", lambda *args: None)
    controller_pid = os.getppid()
    async def scientific_body(self, *, process_id=None):
        await self.set_status(TaskStatus.RUNNING, process_id=process_id)
        self.registry_entry.status = TaskStatus.COMPLETED
        return True
    monkeypatch.setattr(Node, "_execute_node_locally", scientific_body)
    outcome = await node_execution_service.run_node_from_registry_with_outcome(entry, process_id=controller_pid)
    assert outcome == NodeExecutionOutcome(True, "bool")
    assert api.start_attempt.await_count == 2
    assert all(call.kwargs == {"process_id": controller_pid} for call in api.start_attempt.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_pid", [0, -1, 999999999])
async def test_worker_rejects_an_invalid_or_missing_controller_before_bootstrap(monkeypatch, controller_pid):
    initialize = AsyncMock(side_effect=AssertionError("an unowned worker cannot bootstrap"))
    monkeypatch.setattr(run_node_module, "context", SimpleNamespace(initialize=initialize))
    result = await run_node_from_id("unowned-worker", "local", local_worker=controller_pid)
    assert result == RunNodeResult(False, "none", "The local worker no longer belongs to its controller")
    initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_owner_retains_launch_across_transient_bootstrap_failure(monkeypatch, tmp_path):
    from simstack.server_api.http_transport import RunnerAPIError
    entry = SimpleNamespace(id="bootstrap-owner", status=TaskStatus.RETRIEVED, error=None,
        _execution_local=True, parameters=Parameters())
    tasks = _DomainClient(entry)
    initialize = AsyncMock(side_effect=[RunnerAPIError("temporary bootstrap outage", status_code=503), None])
    controller = AsyncMock(return_value=RunNodeResult(True, "bool"))
    monkeypatch.setenv("SIMSTACK_LOCAL_EXECUTION_TOKEN", "scoped-owner-token")
    monkeypatch.setattr(run_node_module, "context", SimpleNamespace(initialize=initialize,
        config=SimpleNamespace(project_root=tmp_path, workdir=tmp_path),
        server=SimpleNamespace(tasks=tasks)))
    monkeypatch.setattr(run_node_module, "_run_local_controller", controller)
    monkeypatch.setattr(run_node_module.asyncio, "sleep", AsyncMock())
    assert await run_claimed_from_id(entry, "local") == RunNodeResult(True, "bool")
    assert initialize.await_count == 2
    assert all(call.kwargs["load_catalogue"] is False for call in initialize.await_args_list)
    assert entry._execution_owner["process_id"] == os.getpid()
    assert entry._execution_owner["execution"] == WorkflowTasksAPI.execution(entry)
    controller.assert_awaited_once()
    assert tasks.saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_local_owner_does_not_retry_revoked_bootstrap_authority(monkeypatch, status):
    from simstack.server_api.http_transport import RunnerAPIError
    initialize = AsyncMock(side_effect=RunnerAPIError("revoked local authority", status_code=status))
    controller = AsyncMock()
    journal = []
    monkeypatch.setenv("SIMSTACK_LOCAL_EXECUTION_TOKEN", "revoked-owner-token")
    monkeypatch.setattr(run_node_module, "context", SimpleNamespace(initialize=initialize))
    monkeypatch.setattr(run_node_module, "_run_local_controller", controller)
    monkeypatch.setattr("simstack.runner_runtime.pending_execution_failures.record_failure", lambda *args, **kwargs: journal.append(kwargs))
    delay = AsyncMock()
    monkeypatch.setattr(run_node_module.asyncio, "sleep", delay)
    result = await run_node_from_id("bootstrap-owner", "local")
    assert not result.success
    initialize.assert_awaited_once()
    delay.assert_not_awaited()
    controller.assert_not_awaited()
    assert journal[0]["phase"] == "bootstrap"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["accepted", "transient", "invalid_json", "rejected_until_disk_recovers"])
@pytest.mark.parametrize("cleanup_pending", [False, True])
async def test_outer_journal_outage_preserves_original_failure_and_stop_gate(monkeypatch, tmp_path, delivery, cleanup_pending):
    import json
    from simstack.runner_runtime import pending_execution_failures as failures
    from simstack.server_api.http_transport import RunnerAPIError
    entry = SimpleNamespace(id="outer-error", status=TaskStatus.RETRIEVED, error=None,
        _execution_local=True, parameters=Parameters())
    tasks = _DomainClient(entry)
    tasks.fail_attempt = AsyncMock(side_effect=RunnerAPIError("already recorded", status_code=409))
    tasks.termination_confirmed = AsyncMock()
    delivered = []
    async def reconcile(**kwargs):
        delivered.append(kwargs)
        tasks.termination_confirmed.assert_not_awaited()
        tasks.fail_attempt.assert_not_awaited()
        if delivery == "rejected_until_disk_recovers":
            raise RunnerAPIError("receipt rejected", status_code=403)
        if len(delivered) == 1 and delivery == "transient":
            raise RunnerAPIError("temporarily unavailable", status_code=503)
        if len(delivered) == 1 and delivery == "invalid_json":
            raise json.JSONDecodeError("invalid response", "<html>", 0)
    tasks.reconcile_attempts = AsyncMock(side_effect=reconcile)
    server = SimpleNamespace(tasks=tasks, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="local", resource="local", token="owner-token", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setenv("SIMSTACK_LOCAL_EXECUTION_TOKEN", "owner-token")
    monkeypatch.setattr(run_node_module, "context", SimpleNamespace(initialize=AsyncMock(), server=server,
        config=SimpleNamespace(project_root=tmp_path, workdir=tmp_path)))
    async def controller(*args, **kwargs):
        entry.status, entry.error = TaskStatus.FAILED, "primary scientific error"
        entry._science_cleanup_pending = cleanup_pending
        raise ValueError("original observation failure")
    monkeypatch.setattr(run_node_module, "_run_local_controller", controller)
    write = failures.record_failure
    def unavailable(*args, **kwargs):
        if delivery != "rejected_until_disk_recovers" or not delivered:
            raise PermissionError("receipt directory is unwritable")
        return write(*args, **kwargs)
    monkeypatch.setattr(failures, "record_failure", unavailable)
    delay = AsyncMock()
    monkeypatch.setattr(run_node_module.asyncio, "sleep", delay)
    result = await run_claimed_from_id(entry, "local")
    assert result == RunNodeResult(False, "exception", "original observation failure")
    assert entry.error == "primary scientific error"
    assert len(delivered) == (1 if delivery == "accepted" else 2)
    assert len({call["request_id"] for call in delivered}) == 1
    receipt = delivered[0]["outcomes"][0]
    assert receipt["request_id"] == delivered[0]["request_id"]
    assert receipt["execution"] == WorkflowTasksAPI.execution(entry)
    assert receipt["phase"] == "execute" and receipt["exit_code"] == 1
    assert receipt["error"] == "original observation failure"
    tasks.fail_attempt.assert_awaited_once_with(entry, "original observation failure")
    assert tasks.termination_confirmed.await_count == int(not cleanup_pending)
    if not cleanup_pending:
        tasks.termination_confirmed.assert_awaited_once_with(entry, "original observation failure")
    records = list(failures.pending_failures())
    assert len(records) == int(delivery == "rejected_until_disk_recovers")
    if records:
        assert records[0][1]["request_id"] == receipt["request_id"]
        assert records[0][1]["rejected_status"] == 403
        assert records[0][1]["error"] == "original observation failure"
    assert delay.await_count == int(delivery != "accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_kind", ["bool", "exception", None])
async def test_controller_preserves_stored_false_boolean_kind(native_controller, protocol_kind):
    scenario = native_controller
    scenario.result = RunNodeResult(False, protocol_kind) if protocol_kind is not None else None
    scenario.returncode = 1
    scenario.tasks.get_outcome.return_value = SimpleNamespace(**{
        **vars(scenario.entry), "status": TaskStatus.FAILED, "return_kind": "bool", "error": "Node returned False"})
    assert await run_node_module._run_local_controller(scenario.entry, "local", scenario.root) == RunNodeResult(False, "bool", "Node returned False")
    scenario.tasks.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_stops_actual_worker_before_propagating_a_decode_failure(monkeypatch, tmp_path):
    from simstack.core.services import task_termination
    entry = SimpleNamespace(id="decode-worker", status=TaskStatus.RUNNING, error=None,
        _execution_generation="generation", _execution_claim_id="claim", _execution_start_id="start")
    stopped = asyncio.Event()
    order = []
    async def communicate():
        await stopped.wait()
        return b"", b""
    async def wait():
        order.append("waited")
        process.returncode = -15
        stopped.set()
    process = SimpleNamespace(pid=12345, returncode=None, communicate=communicate, wait=AsyncMock(side_effect=wait))
    async def stop(original, **kwargs):
        assert original is entry and kwargs["local_process"] is process
        order.append("stopped")
        await process.wait()
    tasks = SimpleNamespace(execution=WorkflowTasksAPI.execution,
        get_outcome=AsyncMock(side_effect=ValueError("invalid response JSON")), termination_confirmed=AsyncMock())
    monkeypatch.setattr(task_termination, "stop_task_execution", AsyncMock(side_effect=stop))
    actual_wait = asyncio.wait
    async def poll(tasks, *, timeout):
        return await actual_wait(tasks, timeout=0)
    monkeypatch.setattr(task_termination.asyncio, "wait", poll)
    with pytest.raises(ValueError, match="invalid response JSON"):
        await task_termination.monitor_task_process(entry, process, server=SimpleNamespace(tasks=tasks),
            workdir=tmp_path, runner_pid=99999)
    order.append("propagated")
    assert order == ["stopped", "waited", "propagated"]
    process.wait.assert_awaited_once()
    tasks.termination_confirmed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 409])
async def test_rejected_fallback_retains_original_attempt_evidence(monkeypatch, tmp_path, status):
    import json
    from simstack.models.document import ObjectId
    from simstack.server_api.http_transport import RunnerAPIError
    entry = SimpleNamespace(id=ObjectId(), _execution_generation="original-generation",
        _execution_claim_id="original-claim", _execution_start_id="original-start", _execution_resource="local")
    requests = SimpleNamespace(server_url="https://example.invalid", authorization="runner", resource="local",
        bootstrap_data={"account_id": "account"})
    api = SimpleNamespace(requests=requests, tasks=SimpleNamespace(execution=WorkflowTasksAPI.execution,
        fail_attempt=AsyncMock(side_effect=RunnerAPIError("superseded or forbidden", status_code=status))))
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=api))
    invocation = Node.__new__(Node)
    invocation.registry_entry = SimpleNamespace(error="primary scientific error")
    await invocation._record_process_failure(entry, "original process was killed", returncode=-9)
    records = [json.loads(path.read_text()) for path in (tmp_path / ".pending-execution-failures").glob("*.json")]
    assert len(records) == 1
    assert records[0]["execution"] == WorkflowTasksAPI.execution(entry)
    assert records[0]["signal"] == 9 and records[0]["error"] == "original process was killed"
    assert invocation.registry_entry.error == "primary scientific error"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process sessions")
async def test_native_controller_drains_orphaned_science_before_ack(tmp_path):
    import sys
    from pathlib import Path
    worker_pid = tmp_path / "grandchild"
    worker = ("import os, subprocess, sys; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(worker_pid)!r}).write_text(str(child.pid)); os._exit(9)")
    script = '''
import asyncio, os, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from simstack.core import run_node
from simstack.core.definitions import TaskStatus
from simstack.runner_runtime import execution_environment
entry = SimpleNamespace(id="original-task", status=TaskStatus.TERMINATING, error="cancel science", return_kind=None)
async def confirmed(*args):
    pid = int(Path(PID_PATH).read_text())
    state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
    assert not state or state.startswith("Z"), "science was acknowledged while still alive: " + state
    entry.status = TaskStatus.FAILED
    return entry
api = SimpleNamespace(tasks=SimpleNamespace(execution=lambda entry: {"task_id": entry.id}, get_outcome=AsyncMock(return_value=entry),
    termination_confirmed=AsyncMock(side_effect=confirmed), fail_attempt=AsyncMock(), reconcile_attempts=AsyncMock()))
run_node.context = SimpleNamespace(server=api, config=SimpleNamespace(project_root=ROOT, workdir=ROOT))
execution_environment.task_project_root = lambda *args: ROOT
execution_environment.task_environment = lambda *args, **kwargs: dict(os.environ)
execution_environment.task_launch_command = lambda *args, **kwargs: [sys.executable, "-c", WORKER]
result = asyncio.run(run_node._run_local_controller(entry, "local", ROOT))
assert not result.success
api.tasks.termination_confirmed.assert_awaited_once()
print("verified stopped science")
'''.replace("PID_PATH", repr(str(worker_pid))).replace("ROOT", repr(str(tmp_path))).replace("WORKER", repr(worker))
    environment = {**os.environ, "PYTHONPATH": str(Path(run_node_module.__file__).resolve().parents[2])}
    controller = await asyncio.create_subprocess_exec(sys.executable, "-c", script, env=environment,
        start_new_session=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(controller.communicate(), 10)
        assert controller.returncode == 0, stderr.decode()
        assert b"verified stopped science" in stdout
    finally:
        if controller.returncode is None:
            controller.kill()
            await controller.wait()
        if worker_pid.exists():
            try:
                os.kill(int(worker_pid.read_text()), 9)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 409])
async def test_controller_keeps_rejected_worker_failure_evidence(native_controller, status):
    import json
    from simstack.server_api.http_transport import RunnerAPIError
    scenario = native_controller
    scenario.result = RunNodeResult(False, "exception", "primary worker error")
    scenario.returncode = 17
    scenario.tasks.get_outcome.return_value = scenario.entry
    scenario.tasks.fail_attempt.side_effect = RunnerAPIError("attempt rejected", status_code=status)
    scenario.tasks.reconcile_attempts.side_effect = RunnerAPIError("evidence rejected", status_code=status)
    result = await run_node_module._run_local_controller(scenario.entry, "local", scenario.root)
    assert result.error == "primary worker error"
    records = [json.loads(path.read_text()) for path in (scenario.root / ".pending-execution-failures").glob("*.json")]
    assert len(records) == 1
    assert records[0]["error"] == "primary worker error"
    assert records[0]["execution"] == WorkflowTasksAPI.execution(scenario.entry)
    assert records[0]["exit_code"] == 17 and records[0]["rejected_status"] == status


@pytest.mark.asyncio
async def test_controller_retains_science_until_cleanup_retry_succeeds(native_controller, monkeypatch):
    from simstack.core.services import task_termination
    scenario = native_controller
    scenario.result = RunNodeResult(False, "exception", "primary science error")
    scenario.returncode = 17
    scenario.tasks.get_outcome.return_value = scenario.entry
    cleanup = AsyncMock(side_effect=[OSError("cannot query owned process membership"), None])
    monkeypatch.setattr(task_termination, "stop_owned_process_group", cleanup)
    async def record(*args, **kwargs):
        assert cleanup.await_count == 2
        return SimpleNamespace(error="primary science error", return_kind="exception")
    scenario.tasks.fail_attempt.side_effect = record
    result = await run_node_module._run_local_controller(scenario.entry, "local", scenario.root)
    assert not result.success and result.error == "primary science error"
    scenario.tasks.fail_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_windows_inline_child_replaces_parent_job_after_breakaway(monkeypatch, tmp_path):
    import importlib
    from simstack.runner_runtime import windows_science_job as jobs
    node_module = importlib.import_module("simstack.core.node")
    entry = SimpleNamespace(id="inline-child", status=TaskStatus.COMPLETED, error=None,
        results_references=[], parameters=Parameters(), _execution_local=False, _execution_launch_kind="inline")
    api = _DomainClient(entry)
    server = SimpleNamespace(tasks=api, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr(node_module, "context", SimpleNamespace(server=server, in_docker=False,
        config=SimpleNamespace(resource="local", project_root=tmp_path, workdir=tmp_path)))
    monkeypatch.setattr(node_module, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(jobs, "child_creationflags", lambda: 0x01000000)
    captured = []
    job = SimpleNamespace(stop=AsyncMock())
    def spawn(command, **kwargs):
        captured.append(kwargs)
        process = _Process(0, encode_run_node_result(RunNodeResult(True, "bool")), streams=kwargs)
        process._simstack_windows_job = job
        return process
    monkeypatch.setattr(jobs, "spawn_job_process", spawn)
    fallback = AsyncMock(side_effect=AssertionError("uncontained Windows science must not be spawned"))
    monkeypatch.setattr(node_module.asyncio, "create_subprocess_exec", fallback)
    assert await _process_node(entry).run_node_as_process() is True
    assert captured[0]["creationflags"] == 0x01000000
    assert captured[0]["retain_in_child"] is True
    assert captured[0]["name"] == jobs.job_name(server, entry)
    job.stop.assert_awaited_once()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_survived", [False, True])
async def test_slurm_container_success_retries_physical_ack(native_controller, monkeypatch, protocol_survived):
    from simstack.server_api.http_transport import RunnerAPIError
    scenario = native_controller
    monkeypatch.setenv("SIMSTACK_SLURM_CONTAINER_EXECUTION", "1")
    monkeypatch.setattr(run_node_module.os, "setsid", lambda: None)
    scenario.server.slurm = SimpleNamespace(container_intent=AsyncMock())
    if not protocol_survived:
        scenario.result = None
        scenario.tasks.get_outcome.return_value = SimpleNamespace(status=TaskStatus.COMPLETED, return_kind="bool")
    scenario.tasks.termination_confirmed.side_effect = [RunnerAPIError("lost ACK", status_code=503), None]
    result = await run_node_module._run_local_controller(scenario.entry, "local", scenario.root, in_docker=True)
    assert result == RunNodeResult(True, "bool")
    assert scenario.tasks.termination_confirmed.await_count == 2
    scenario.tasks.fail_attempt.assert_not_awaited()
    scenario.server.slurm.container_intent.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [17, -9])
@pytest.mark.parametrize("stored_status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
@pytest.mark.parametrize("delivery", ["accepted", "offline", "forbidden", "conflict", "invalid_json"])
async def test_inline_terminal_exit_retains_evidence_without_changing_science(
    monkeypatch, tmp_path, code, stored_status, delivery,
):
    from simstack.models.document import ObjectId
    from simstack.runner_runtime.pending_execution_failures import pending_failures
    from simstack.server_api.http_transport import RunnerAPIError
    launch = SimpleNamespace(id=ObjectId(), status=TaskStatus.RUNNING, error=None,
        _execution_generation="original-generation", _execution_claim_id="original-claim")
    stored = SimpleNamespace(id=launch.id, status=stored_status,
        error="primary scientific error" if stored_status == TaskStatus.FAILED else None,
        return_kind="exception" if stored_status == TaskStatus.FAILED else "bool")
    requests = SimpleNamespace(server_url="https://server.example/api", authorization="runner", resource="local",
        bootstrap_data={"account_id": "test-account"})
    async def read(original):
        assert original is launch
        pending, = pending_failures()
        assert pending[1]["execution"] == WorkflowTasksAPI.execution(launch)
        return stored
    tasks = SimpleNamespace(get_outcome=AsyncMock(side_effect=read), fail_attempt=AsyncMock(),
        reconcile_attempts=AsyncMock(), execution=WorkflowTasksAPI.execution)
    if delivery != "accepted":
        error = (ValueError("invalid reply") if delivery == "invalid_json" else
            RunnerAPIError("delivery unavailable", status_code={"offline": 503, "forbidden": 403, "conflict": 409}[delivery]))
        tasks.reconcile_attempts.side_effect = error
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=SimpleNamespace(tasks=tasks, requests=requests),
        in_docker=False, config=SimpleNamespace(resource="local", project_root=tmp_path)))
    async def spawn(*args, **kwargs):
        protocol = RunNodeResult(stored_status == TaskStatus.COMPLETED,
            "bool" if stored_status == TaskStatus.COMPLETED else "exception", "different protocol error")
        return _Process(code, encode_run_node_result(protocol), streams=kwargs)
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", spawn)
    node = _process_node(launch)
    if stored_status == TaskStatus.COMPLETED:
        assert await node.run_node_as_process() is True
        assert stored.error is None and stored.return_kind == "bool"
    else:
        with pytest.raises(RuntimeError, match="primary scientific error"):
            await node.run_node_as_process()
        assert stored.error == "primary scientific error" and stored.return_kind == "exception"
    assert stored.status == stored_status
    tasks.fail_attempt.assert_not_awaited()
    tasks.reconcile_attempts.assert_awaited_once()
    receipt, = tasks.reconcile_attempts.await_args.kwargs["outcomes"]
    assert receipt["execution"] == WorkflowTasksAPI.execution(launch)
    assert receipt["exit_code"] == (code if code > 0 else None)
    assert receipt["signal"] == (-code if code < 0 else None)
    records = list(pending_failures())
    assert len(records) == int(delivery != "accepted")
    if records:
        assert records[0][1]["request_id"] == receipt["request_id"]
        assert records[0][1]["execution"] == receipt["execution"]
        if delivery in {"forbidden", "conflict"}:
            assert records[0][1]["rejected_status"] == {"forbidden": 403, "conflict": 409}[delivery]


@pytest.mark.asyncio
async def test_inline_exit_outage_keeps_one_original_receipt_before_outcome_read(monkeypatch, tmp_path):
    from simstack.models.document import ObjectId
    from simstack.runner_runtime.pending_execution_failures import pending_failures
    from simstack.server_api.http_transport import RunnerAPIError
    launch = SimpleNamespace(id=ObjectId(), status=TaskStatus.RUNNING, error=None,
        _execution_generation="original-generation", _execution_claim_id="original-claim")
    async def read(original):
        record, = pending_failures()
        assert record[1]["signal"] == 9
        raise RunnerAPIError("outcome unavailable", status_code=503)
    tasks = SimpleNamespace(execution=WorkflowTasksAPI.execution, get_outcome=AsyncMock(side_effect=read),
        fail_attempt=AsyncMock(side_effect=RunnerAPIError("failure unavailable", status_code=503)),
        reconcile_attempts=AsyncMock(side_effect=RunnerAPIError("reconcile unavailable", status_code=503)))
    server = SimpleNamespace(tasks=tasks, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=server,
        in_docker=False, config=SimpleNamespace(resource="local", project_root=tmp_path)))
    async def spawn(*args, **kwargs):
        return _Process(-9, encode_run_node_result(RunNodeResult(False, "exception", "primary protocol error")), streams=kwargs)
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="primary protocol error"):
        await _process_node(launch).run_node_as_process()
    record, = pending_failures()
    assert record[1]["execution"] == WorkflowTasksAPI.execution(launch)
    assert record[1]["signal"] == 9 and record[1]["error"] == "primary protocol error"
    assert record[1]["request_id"] == tasks.reconcile_attempts.await_args.kwargs["request_id"]
    tasks.fail_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_late_exit_receipt_never_uses_new_canonical_generation(monkeypatch, tmp_path):
    from simstack.models.document import ObjectId
    launch = SimpleNamespace(id=ObjectId(), status=TaskStatus.RUNNING, error=None,
        _execution_generation="old-generation", _execution_claim_id="old-claim")
    current = SimpleNamespace(id=launch.id, status=TaskStatus.RUNNING, error=None,
        _execution_generation="new-generation", _execution_claim_id="new-claim", _execution_start_id="new-start")
    historical = SimpleNamespace(id=launch.id, status=TaskStatus.COMPLETED, error=None, return_kind="bool")
    tasks = SimpleNamespace(execution=WorkflowTasksAPI.execution,
        get_outcome=AsyncMock(return_value=historical), get_task=AsyncMock(return_value=current),
        fail_attempt=AsyncMock(), reconcile_attempts=AsyncMock())
    server = SimpleNamespace(tasks=tasks, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=server,
        in_docker=False, config=SimpleNamespace(resource="local", project_root=tmp_path)))
    async def spawn(*args, **kwargs):
        return _Process(17, "", streams=kwargs)
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", spawn)
    assert await _process_node(launch).run_node_as_process() is True
    receipt, = tasks.reconcile_attempts.await_args.kwargs["outcomes"]
    assert receipt["execution"] == WorkflowTasksAPI.execution(launch)
    assert receipt["execution"] != WorkflowTasksAPI.execution(current)
    assert current.status == TaskStatus.RUNNING and current.error is None
    tasks.get_task.assert_not_awaited()
    tasks.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("journal_recovers", [False, True])
async def test_inline_exit_retained_when_durable_sinks_recover(monkeypatch, tmp_path, journal_recovers):
    from simstack.models.document import ObjectId
    from simstack.runner_runtime import pending_execution_failures as failures
    from simstack.server_api.http_transport import RunnerAPIError
    launch = SimpleNamespace(id=ObjectId(), status=TaskStatus.COMPLETED, error=None, return_kind="bool",
        _execution_generation="generation", _execution_claim_id="claim")
    api = _DomainClient(launch)
    api.reconcile_attempts = AsyncMock(side_effect=RunnerAPIError("offline", status_code=503) if journal_recovers else
        [RunnerAPIError("offline", status_code=503), {"claims": [], "outcomes": []}])
    server = SimpleNamespace(tasks=api, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=server,
        in_docker=False, config=SimpleNamespace(resource="local", project_root=tmp_path)))
    real_record = failures.record_failure
    writes = 0
    def write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes > 1 and journal_recovers:
            return real_record(*args, **kwargs)
        raise PermissionError("journal unavailable")
    monkeypatch.setattr(failures, "record_failure", write)
    monkeypatch.setattr(run_node_module.asyncio, "sleep", AsyncMock())
    async def spawn(*args, **kwargs):
        return _Process(17, "", streams=kwargs)
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", spawn)
    assert await _process_node(launch).run_node_as_process() is True
    calls = api.reconcile_attempts.await_args_list
    assert len(calls) == (1 if journal_recovers else 2)
    assert len({call.kwargs["request_id"] for call in calls}) == 1
    assert len(list(failures.pending_failures())) == int(journal_recovers)
    assert launch.status == TaskStatus.COMPLETED and launch.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["process_group", "windows_job"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("offline", [False, True])
async def test_inline_exit_is_durable_before_owned_cleanup_without_false_stop_ack(
    monkeypatch, tmp_path, backend, cleanup_fails, offline,
):
    from simstack.models.document import ObjectId
    from simstack.runner_runtime.pending_execution_failures import pending_failures
    from simstack.server_api.http_transport import RunnerAPIError
    launch = SimpleNamespace(id=ObjectId(), status=TaskStatus.COMPLETED, error=None, return_kind="bool",
        _execution_generation="generation", _execution_claim_id="claim")
    api = _DomainClient(launch)
    api.termination_confirmed = AsyncMock()
    api.reconcile_attempts = AsyncMock(side_effect=RunnerAPIError("offline", status_code=503) if offline else None)
    server = SimpleNamespace(tasks=api, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=server,
        in_docker=False, config=SimpleNamespace(resource="local", project_root=tmp_path, workdir=tmp_path)))
    cleaned = []
    async def cleanup(*args, **kwargs):
        pending, = pending_failures()
        assert pending[1]["signal"] == 9
        assert pending[1]["execution"] == WorkflowTasksAPI.execution(launch)
        cleaned.append(True)
        if cleanup_fails and len(cleaned) == 1:
            raise OSError("owned cleanup unavailable")
    monkeypatch.setattr("simstack.core.services.task_termination.stop_task_execution", cleanup)
    monkeypatch.setattr("simstack.core.services.task_termination.stop_owned_process_group", cleanup)
    async def spawn(*args, **kwargs):
        process = _Process(-9, "", streams=kwargs)
        if backend == "process_group":
            process._simstack_process_group = True
        else:
            process._simstack_windows_job = object()
        return process
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("simstack.core.node.asyncio.sleep", AsyncMock())
    assert await _process_node(launch).run_node_as_process() is True
    assert cleaned
    receipt, = api.reconcile_attempts.await_args.kwargs["outcomes"]
    assert receipt["signal"] == 9
    assert "execution_stopped" not in receipt
    api.termination_confirmed.assert_not_awaited()
    assert launch.status == TaskStatus.COMPLETED and launch.error is None
    assert len(list(pending_failures())) == int(offline)


@pytest.mark.asyncio
async def test_verified_inline_cleanup_retries_unpersisted_receipt_release(monkeypatch, tmp_path):
    from simstack.models.document import ObjectId
    from simstack.runner_runtime import pending_execution_failures as failures
    from simstack.server_api.http_transport import RunnerAPIError
    launch = SimpleNamespace(id=ObjectId(), status=TaskStatus.COMPLETED, error=None, return_kind="bool",
        _execution_generation="generation", _execution_claim_id="claim")
    api = _DomainClient(launch)
    api.reconcile_attempts = AsyncMock(side_effect=RunnerAPIError("offline", status_code=503))
    server = SimpleNamespace(tasks=api, requests=SimpleNamespace(server_url="https://server.example/api",
        authorization="runner", resource="local", bootstrap_data={"account_id": "test-account"}))
    monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(server=server,
        in_docker=False, config=SimpleNamespace(resource="local", project_root=tmp_path, workdir=tmp_path)))
    original = failures.record_failure
    writes = []
    def write(*args, **kwargs):
        writes.append(dict(kwargs))
        if len(writes) in {2, 3}:
            raise PermissionError("receipt update temporarily unavailable")
        return original(*args, **kwargs)
    monkeypatch.setattr(failures, "record_failure", write)
    monkeypatch.setattr("simstack.core.node.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("simstack.core.services.task_termination.stop_task_execution", AsyncMock())
    async def spawn(*args, **kwargs):
        process = _Process(17, "", streams=kwargs)
        process._simstack_process_group = True
        return process
    monkeypatch.setattr("simstack.core.node.asyncio.create_subprocess_exec", spawn)
    assert await _process_node(launch).run_node_as_process() is True
    path, stored = next(failures.pending_failures())
    assert stored["exit_code"] == 17 and "cleanup" not in stored
    assert len(writes) == 4 and len({value["request_id"] for value in writes}) == 1
    assert len(api.reconcile_attempts.await_args_list) == 2
