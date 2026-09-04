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


class _Process:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()

    async def communicate(self):
        return self._stdout, self._stderr


class _Database:
    def __init__(self, entry) -> None:
        self.entry = entry
        self.saved = []

    async def load_task_by_id(self, task_id):
        return self.entry

    async def save(self, entry):
        self.saved.append(entry)
        return entry


def _process_node(entry, parameters: Parameters | None = None) -> Node:
    execution_node = Node.__new__(Node)
    execution_node.name = "process_probe"
    execution_node.registry_entry = entry
    execution_node.parameters = parameters or Parameters(resource="self")
    return execution_node


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
async def test_process_for_self_uses_actual_resource_and_preserves_true(
    monkeypatch, tmp_path
):
    entry = SimpleNamespace(
        id="task-one",
        status=TaskStatus.COMPLETED,
        error=None,
        results_references=[],
    )
    database = _Database(entry)
    captured = []
    host_python_path = "/host-only/simstack/src"
    image_python_path = "/image/site-packages"
    monkeypatch.setenv("PYTHONPATH", image_python_path)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _Process(0, encode_run_node_result(RunNodeResult(True, "bool")))

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
            db=database,
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
    assert child_env["SIMSTACK_DB_CONNECTION_STRING"] == (
        "mongodb://user:password@db:27017/"
    )
    assert child_env["SIMSTACK_DB_DATABASE"] == "child-database"
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
    database = _Database(entry)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return _Process(0, encode_run_node_result(RunNodeResult(True, "none")))

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=False,
            config=SimpleNamespace(
                resource="local",
                project_root=tmp_path,
                connection_string=None,
            ),
            db=database,
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
    database = _Database(entry)
    sentinel = SimpleNamespace(kind=return_kind)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return _Process(
            0, encode_run_node_result(RunNodeResult(True, return_kind))
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
            db=database,
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(Node, "load_results", fake_load_results)

    assert await _process_node(entry).run_node_as_process() is sentinel


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
    database = _Database(entry)
    protocol = encode_run_node_result(
        RunNodeResult(False, "exception", f"child failed at {secret}")
    )

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return _Process(
            1,
            f"{'x' * 5000}{secret}\n{protocol}",
            f"{'y' * 5000}{secret}",
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
            db=database,
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
    assert len(entry.error) < 4096
    assert entry.status == TaskStatus.FAILED
    assert database.saved[-1] is entry


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
    database = _Database(entry)

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
            db=database,
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
    assert database.saved[-1] is entry


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
        parameters=Parameters(in_docker=False),
        status=TaskStatus.RETRIEVED,
        error=None,
        return_kind=None,
    )
    database = _Database(entry)

    async def initialize(**kwargs):
        return None

    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            config=SimpleNamespace(connection_string=None),
            db=database,
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.run_node_from_registry_with_outcome",
        AsyncMock(side_effect=SystemExit()),
    )

    result = await run_node_from_id("task-id", "local")

    assert result == RunNodeResult(False, "exception", "SystemExit")
    assert entry.status == TaskStatus.FAILED
    assert entry.error == "SystemExit"
    assert entry.return_kind == "exception"
    assert database.saved[-1] is entry


@pytest.mark.asyncio
async def test_false_return_kind_is_preserved_as_failed_child_outcome(monkeypatch):
    entry = SimpleNamespace(
        parameters=Parameters(in_docker=False),
        status=TaskStatus.FAILED,
        error=None,
    )
    database = _Database(entry)

    async def initialize(**kwargs):
        return None

    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            config=SimpleNamespace(connection_string=None),
            db=database,
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.run_node_from_registry_with_outcome",
        AsyncMock(return_value=NodeExecutionOutcome(False, "bool")),
    )

    result = await run_node_from_id("task-id", "local")

    assert result == RunNodeResult(False, "bool")


@pytest.mark.asyncio
async def test_in_container_context_uses_shared_non_root_workdir(monkeypatch):
    entry = SimpleNamespace(
        parameters=Parameters(in_docker=True),
        status=TaskStatus.RETRIEVED,
        error=None,
    )
    database = _Database(entry)
    init_kwargs = {}

    async def initialize(**kwargs):
        init_kwargs.update(kwargs)

    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            config=SimpleNamespace(connection_string=None),
            db=database,
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.run_node_from_registry_with_outcome",
        AsyncMock(return_value=NodeExecutionOutcome(True, "model")),
    )

    result = await run_node_from_id("task-id", "local", in_docker=True)

    assert result == RunNodeResult(True, "model")
    assert init_kwargs["workdir"] == CONTAINER_WORKDIR
    assert not CONTAINER_WORKDIR.startswith("/root/")


@pytest.mark.asyncio
async def test_docker_child_return_kind_is_forwarded(monkeypatch):
    entry = SimpleNamespace(
        parameters=Parameters(in_docker=True),
        status=TaskStatus.COMPLETED,
        error=None,
        results_references=[object(), object()],
    )
    database = _Database(entry)

    async def initialize(**kwargs):
        return None

    monkeypatch.setattr(
        "simstack.core.run_node.context",
        SimpleNamespace(
            initialize=initialize,
            config=SimpleNamespace(connection_string=None),
            db=database,
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.run_docker_with_outcome",
        AsyncMock(return_value=DockerRunResult(True, "multiple")),
    )

    assert await run_node_from_id("task-id", "local") == RunNodeResult(
        True, "multiple"
    )


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
    database = _Database(completed)
    execution_node = _process_node(completed, Parameters(resource="remote"))
    execution_node.name = "remote_probe"
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            config=SimpleNamespace(connection_string=None),
            db=database,
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
