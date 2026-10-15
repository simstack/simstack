from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.run_docker import run_docker
from simstack.core.run_node import run_node_from_id
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.core.services.runner_manager import RunnerManager, runner_pid_file
from simstack.core.services.slurm_status_service import SlurmStatusService
from simstack.core.submit_node import submit_node
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters, Queue, Resource
from simstack.runner_api import ClaimedTask, RunnerApiError
from simstack.runner_protocol import RUNNER_TASK_WIRE_FIELDS
from simstack.runner_runtime.environment import (
    is_blocked_runner_environment,
    sanitize_runner_process_environment,
    sanitized_runner_environment,
    sanitized_launcher_environment,
)
from simstack.runner_runtime.config import runner_task_workdir
from simstack.runner_runtime.execution_adapter import RunnerExecutionAdapterConfig
from simstack.runner_runtime.slurm import get_job_info, run_squeue_for_job


_DATABASE_SECRET = "mongodb://sentinel-secret"


@pytest.fixture(autouse=True)
def isolate_mock_worker_process_groups(monkeypatch):
    # This module stubs subprocess creation. Never signal its synthetic PID.
    monkeypatch.setattr(
        "simstack.runner_runtime.processes.signal_worker_tree", MagicMock()
    )


def _finished_process(*, returncode=0, stdout=b"", stderr=b""):
    streams = []
    for value in (stdout, stderr):
        stream = asyncio.StreamReader()
        stream.feed_data(value)
        stream.feed_eof()
        streams.append(stream)
    return SimpleNamespace(
        pid=1234, returncode=returncode, stdout=streams[0], stderr=streams[1],
    )


def _install_database_environment(monkeypatch):
    monkeypatch.setenv("mongodb_connection_string", _DATABASE_SECRET)
    monkeypatch.setenv("SIMSTACK_DB_CONNECTION_STRING", _DATABASE_SECRET)
    monkeypatch.setenv("simstack_test_db_connection_string", _DATABASE_SECRET)
    monkeypatch.setenv("DATABASE_URL", _DATABASE_SECRET)
    monkeypatch.setenv("Db_Name", "sentinel-database")
    monkeypatch.setenv("SIMSTACK_CONFIG_FILE", "/secret/simstack.toml")
    monkeypatch.setenv("APPTAINERENV_MONGODB_URI", _DATABASE_SECRET)
    monkeypatch.setenv("PGPASSWORD", "postgres-secret")
    monkeypatch.setenv("REDIS_URL", "redis://sentinel")
    monkeypatch.setenv("TOTALLY_CUSTOM_SECRET", "custom-secret")


def _assert_database_environment_removed(environment):
    assert _DATABASE_SECRET not in environment.values()
    assert "sentinel-database" not in environment.values()
    assert "/secret/simstack.toml" not in environment.values()
    assert "postgres-secret" not in environment.values()
    assert "redis://sentinel" not in environment.values()
    assert "custom-secret" not in environment.values()
    assert not any(is_blocked_runner_environment(name) for name in environment)


def _registry(*, queue: Queue) -> NodeRegistry:
    parameters = Parameters(queue=queue)
    return NodeRegistry(
        name="worker_node",
        status=TaskStatus.RETRIEVED,
        function_hash="function-hash",
        arg_hash="argument-hash",
        func_mapping="package.worker_node",
        parameters=parameters,
    )


def _execution_adapter(
    *,
    program: dict[str, object] | None = None,
    setup: dict[str, object] | None = None,
) -> RunnerExecutionAdapterConfig:
    return RunnerExecutionAdapterConfig.from_mapping(
        {
            "resource": {
                "program": {"worker_node": program or {}},
                "setup": setup or {},
            }
        },
        resource="resource",
    )


def _task_bundle(registry: NodeRegistry) -> dict[str, object]:
    document = registry.model_dump(mode="json")
    task = {field: document.get(field) for field in RUNNER_TASK_WIRE_FIELDS}
    task.update(
        {
            "task_schema_version": 1,
            "created_at": "2026-08-17T12:00:00Z",
            "artifact_plan": {
                "required": False,
                "mappings": [],
                "child_artifacts": [],
            },
            "info_files": {"elements": []},
            "inputs": [],
            "results": [],
        }
    )
    return task


def test_worker_environment_uses_strict_allowlist(monkeypatch):
    _install_database_environment(monkeypatch)
    monkeypatch.setenv("SafeProgramLicense", "allowed")

    environment = sanitized_runner_environment(
        {"SIMSTACK_RUNNER_TOKEN": "execution-token"},
        allowed_environment_names=("SafeProgramLicense",),
    )

    _assert_database_environment_removed(environment)
    assert environment["SafeProgramLicense"] == "allowed"
    assert environment["SIMSTACK_RUNNER_TOKEN"] == "execution-token"


@pytest.mark.parametrize("build", [sanitized_runner_environment, sanitized_launcher_environment])
@pytest.mark.parametrize("packaged", [False, True])
def test_only_packaged_python_bootstrap_pins_locale_against_inherited_overrides(build, packaged):
    inherited = {
        "PATH": "/usr/bin", "LANG": "ru_RU.KOI8-R",
        "LC_ALL": "fr_FR.ISO8859-1", "LC_CTYPE": "de_DE.ISO8859-1",
    }
    environment = build(
        {"SIMSTACK_RUNNER_TOKEN": "claim-token"}, source=inherited,
        allowed_environment_names=("LANG", "LC_ALL", "LC_CTYPE"),
        workflow_bootstrap=packaged,
    )
    assert environment["LANG"] == ("C.UTF-8" if packaged else inherited["LANG"])
    assert environment["LC_ALL"] == ("C.UTF-8" if packaged else inherited["LC_ALL"])
    assert environment["LC_CTYPE"] == inherited["LC_CTYPE"]


def _install_hostile_locale(monkeypatch, registry, packaged):
    monkeypatch.setenv("LANG", "ru_RU.KOI8-R")
    monkeypatch.setenv("LC_ALL", "fr_FR.ISO8859-1")
    monkeypatch.setenv("LC_CTYPE", "de_DE.ISO8859-1")
    if packaged:
        registry.workflow_revision = "a" * 64
        registry.workflow_package = "example.package"
        registry.workflow_installation_id = "installation"
    # Cache integrity is exercised separately. These process stubs only inspect
    # the backend's propagation of the already selected package bootstrap flag.
    monkeypatch.setattr(
        "simstack.runner_runtime.workflow_installation.packaged_worker_command",
        lambda command, registry, **kwargs: command,
    )
    monkeypatch.setattr(
        "simstack.runner_runtime.workflow_installation.ensure_claimed_package_runtime",
        AsyncMock(),
    )


def _assert_bootstrap_locale(environment, packaged):
    assert environment["LANG"] == ("C.UTF-8" if packaged else "ru_RU.KOI8-R")
    assert environment["LC_ALL"] == ("C.UTF-8" if packaged else "fr_FR.ISO8859-1")


@pytest.mark.asyncio
async def test_worker_reports_initialization_failure_before_context_exists(monkeypatch, tmp_path):
    failure_api = SimpleNamespace(fail_task=AsyncMock())

    class FailedContext:
        initialize_runner = AsyncMock(side_effect=RuntimeError("bootstrap unavailable"))

        @property
        def runner_api(self):
            raise RuntimeError("not initialized")

    monkeypatch.setattr("simstack.core.run_node.context", FailedContext())
    monkeypatch.setattr("simstack.core.run_node.ExecutionRunnerClient.from_environment", lambda: failure_api)
    monkeypatch.setattr("simstack.core.run_node.worker_environment_allowlist_from_environment", lambda: ())
    monkeypatch.setattr("simstack.core.run_node.sanitize_worker_process_environment", lambda **kwargs: None)
    result = await run_node_from_id("0123456789abcdef01234567", "resource", tmp_path, claim_id="claim", attempt=2)
    assert result is False
    failure_api.fail_task.assert_awaited_once_with(
        "0123456789abcdef01234567", claim_id="claim", attempt=2,
        error="RuntimeError", message="bootstrap unavailable",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("packaged", [False, True])
async def test_inline_child_uses_its_package_binding_to_pin_bootstrap_locale(
    monkeypatch, tmp_path, packaged
):
    from simstack.core.runner_execution import inline_child
    _install_database_environment(monkeypatch)
    monkeypatch.setenv("LANG", "ru_RU.KOI8-R")
    monkeypatch.setenv("LC_ALL", "fr_FR.ISO8859-1")
    monkeypatch.setenv("LC_CTYPE", "de_DE.ISO8859-1")
    claim = ClaimedTask(
        task={"id": "507f1f77bcf86cd799439011", "workflow_revision": "a" * 64 if packaged else None},
        claim_id="child-claim", attempt=1,
        lease_until="2026-10-09T16:00:00Z",
        execution_token="child-only-token",
        execution_token_expires_at="2026-10-09T16:00:00Z",
    )
    monkeypatch.setattr(
        inline_child, "context", SimpleNamespace(
            config=SimpleNamespace(resource="local", project_root=tmp_path, workdir=tmp_path / "work"),
            runner_api=SimpleNamespace(server_url="https://server.example/api"),
        ),
    )
    monkeypatch.setattr(inline_child, "worker_environment_allowlist_from_environment", lambda: ())
    create = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(inline_child.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(inline_child, "supervise_worker", AsyncMock(return_value=SimpleNamespace(returncode=0)))
    await inline_child.execute_inline_child(claim)
    environment = create.await_args.kwargs["env"]
    _assert_bootstrap_locale(environment, packaged)
    _assert_database_environment_removed(environment)
    assert environment["SIMSTACK_RUNNER_TOKEN"] == "child-only-token"


def test_supervisor_process_environment_is_an_allowlist_not_a_secret_denylist():
    environment = {
        "PATH": "/usr/bin",
        "HOME": "/runner",
        "LC_ALL": "C",
        "SIMSTACK_SERVER_URL": "https://simstack.example/api",
        "SIMSTACK_RUNNER_TOKEN": "runner-token",
        "SIMSTACK_TASK_ID": "stale-task",
        "SIMSTACK_TASK_CLAIM_ID": "stale-claim",
        "SIMSTACK_TASK_ATTEMPT": "99",
        "SIMSTACK_PROJECT_ROOT": "/stale/project",
        "SIMSTACK_RUNNER_WORKDIR": "/stale/workdir",
        "SafeProgramLicense": "allowed",
        "TOTALLY_CUSTOM_SECRET": "must-disappear",
        "PGPASSWORD": "must-disappear-too",
    }

    removed = sanitize_runner_process_environment(
        allowed_environment_names=("SafeProgramLicense",),
        environment=environment,
    )

    assert "TOTALLY_CUSTOM_SECRET" in removed
    assert "PGPASSWORD" in removed
    assert "SIMSTACK_TASK_ID" in removed
    assert "SIMSTACK_TASK_CLAIM_ID" in removed
    assert "SIMSTACK_TASK_ATTEMPT" in removed
    assert "SIMSTACK_PROJECT_ROOT" in removed
    assert "SIMSTACK_RUNNER_WORKDIR" in removed
    assert environment == {
        "PATH": "/usr/bin",
        "HOME": "/runner",
        "LC_ALL": "C",
        "SIMSTACK_SERVER_URL": "https://simstack.example/api",
        "SIMSTACK_RUNNER_TOKEN": "runner-token",
        "SafeProgramLicense": "allowed",
    }


@pytest.mark.asyncio
async def test_run_node_loads_task_by_claim_before_execution(monkeypatch, tmp_path):
    _install_database_environment(monkeypatch)
    task_id = "507f1f77bcf86cd799439011"
    get_task = AsyncMock(
        return_value=ClaimedTask(
            task={"id": task_id},
            claim_id="claim-9",
            attempt=5,
            lease_until="2026-08-14T16:00:00Z",
            execution_token="execution-token-9",
            execution_token_expires_at="2026-09-14T16:00:00Z",
        )
    )
    fake_context = SimpleNamespace(
        initialize_runner=AsyncMock(),
        runner_api=SimpleNamespace(get_task=get_task),
    )
    execute = AsyncMock(return_value=True)
    monkeypatch.setattr("simstack.core.run_node.context", fake_context)
    monkeypatch.setattr("simstack.core.run_node.execute_claimed_task", execute)
    sanitize = MagicMock()
    discard = MagicMock()
    monkeypatch.setattr(
        "simstack.core.run_node.worker_environment_allowlist_from_environment",
        lambda: (),
    )
    monkeypatch.setattr(
        "simstack.core.run_node.sanitize_worker_process_environment", sanitize
    )
    monkeypatch.setattr(
        "simstack.core.run_node.discard_runner_token_from_environment", discard
    )

    succeeded = await run_node_from_id(
        task_id,
        "cluster",
        tmp_path,
        claim_id="claim-9",
        attempt=5,
    )

    assert succeeded is True
    fake_context.initialize_runner.assert_awaited_once_with(
        resource="cluster",
        project_root=tmp_path,
        with_file_transfer=True,
        task_id=task_id,
        claim_id="claim-9",
        attempt=5,
    )
    get_task.assert_awaited_once_with(task_id, claim_id="claim-9", attempt=5)
    execute.assert_awaited_once_with({"id": task_id}, claim_id="claim-9", attempt=5)
    sanitize.assert_called_once_with(allowed_environment_names=())
    discard.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("packaged", [False, True])
async def test_docker_worker_receives_only_api_control_plane_environment(
    monkeypatch, tmp_path, packaged
):
    _install_database_environment(monkeypatch)
    registry = _registry(queue=Queue.DEFAULT)
    _install_hostile_locale(monkeypatch, registry, packaged)
    registry.parameters.in_docker = True
    supervisor_api = SimpleNamespace(
        server_url="https://simstack.example/api",
        runner_token="supervisor-secret",
    )
    execution_api = SimpleNamespace(
        server_url="https://simstack.example/api",
        runner_token="execution-secret",
        renew_lease=AsyncMock(return_value={}),
    )
    fake_context = SimpleNamespace(
        runner_api=supervisor_api,
        config=SimpleNamespace(
            resource="local", workdir=tmp_path / "work", project_root=tmp_path
        ),
        runner_adapter=_execution_adapter(
            program={
                "container_backend": "docker",
                "container_command": ["container-wrapper", "docker-executable"],
                "container_worker_command": ["configured-run-node"],
                "worker_network_guard_command": ["network-guard", "--"],
                "worker_environment_allowlist": [],
                "container_image": "configured-worker:latest",
                "container_project_root": "/workspace/source",
                "container_workdir": "/workspace/tasks",
            },
        ),
    )
    monkeypatch.setattr("simstack.core.run_docker.context", fake_context)

    process = _finished_process(stdout=b"ok")
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "simstack.core.run_docker.asyncio.create_subprocess_exec", create_process
    )

    assert (
        await run_docker(
            registry,
            claim_id="claim-1",
            attempt=3,
            lease_seconds=120,
            runner_api=execution_api,
        )
        is True
    )

    command = create_process.await_args.args
    environment = create_process.await_args.kwargs["env"]
    _assert_database_environment_removed(environment)
    _assert_bootstrap_locale(environment, packaged)
    assert not any("SIMSTACK_DB_" in argument for argument in command)
    assert "runner-secret" not in command
    assert environment["SIMSTACK_SERVER_URL"] == "https://simstack.example/api"
    assert environment["SIMSTACK_RUNNER_TOKEN"] == "execution-secret"
    assert "supervisor-secret" not in environment.values()
    assert environment["SIMSTACK_TASK_ID"] == str(registry.id)
    assert environment["SIMSTACK_TASK_CLAIM_ID"] == "claim-1"
    assert environment["SIMSTACK_TASK_ATTEMPT"] == "3"
    assert environment["SIMSTACK_PROJECT_ROOT"] == "/workspace/source"
    assert environment["SIMSTACK_RUNNER_WORKDIR"] == "/workspace/tasks"
    assert command[:2] == (
        "container-wrapper",
        "docker-executable",
    )
    image_index = command.index("configured-worker:latest")
    assert command[image_index + 1 : image_index + 4] == (
        "network-guard",
        "--",
        "configured-run-node",
    )
    assert f"{tmp_path.resolve()}:/workspace/source:ro" in command
    assert f"{(tmp_path / 'work').resolve()}:/workspace/tasks" in command


@pytest.mark.asyncio
@pytest.mark.parametrize("packaged", [False, True])
async def test_apptainer_worker_uses_clean_environment(monkeypatch, tmp_path, packaged):
    _install_database_environment(monkeypatch)
    registry = _registry(queue=Queue.DEFAULT)
    _install_hostile_locale(monkeypatch, registry, packaged)
    registry.parameters.in_docker = True
    fake_context = SimpleNamespace(
        runner_api=SimpleNamespace(
            server_url="https://simstack.example/api",
            runner_token="runner-secret",
        ),
        config=SimpleNamespace(
            resource="cluster", workdir=tmp_path / "work", project_root=tmp_path
        ),
        runner_adapter=_execution_adapter(
            program={
                "container_backend": "apptainer",
                "container_command": ["container-wrapper", "apptainer-executable"],
                "container_worker_command": ["configured-run-node"],
                "worker_network_guard_command": ["network-guard", "--"],
                "worker_environment_allowlist": [],
                "container_image": "configured-worker.sif",
                "container_project_root": "/workspace/source",
                "container_workdir": "/workspace/tasks",
            },
        ),
    )
    monkeypatch.setattr("simstack.core.run_docker.context", fake_context)
    execution_api = SimpleNamespace(
        server_url="https://simstack.example/api",
        runner_token="execution-secret",
        renew_lease=AsyncMock(return_value={}),
    )
    process = _finished_process()
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "simstack.core.run_docker.asyncio.create_subprocess_exec", create_process
    )

    assert (
        await run_docker(
            registry,
            claim_id="claim-2",
            attempt=4,
            lease_seconds=120,
            runner_api=execution_api,
        )
        is True
    )

    command = create_process.await_args.args
    environment = create_process.await_args.kwargs["env"]
    assert "--cleanenv" in command
    _assert_bootstrap_locale(environment, packaged)
    assert environment["APPTAINERENV_LANG"] == environment["LANG"]
    assert environment["APPTAINERENV_LC_ALL"] == environment["LC_ALL"]
    _assert_database_environment_removed(environment)
    assert environment["APPTAINERENV_SIMSTACK_RUNNER_TOKEN"] == "execution-secret"
    assert not any("MONGO" in name.upper() for name in environment)
    assert command[:2] == (
        "container-wrapper",
        "apptainer-executable",
    )
    image_index = command.index("configured-worker.sif")
    assert command[image_index + 1 : image_index + 4] == (
        "network-guard",
        "--",
        "configured-run-node",
    )
    assert "/workspace/source" in command
    assert "/workspace/tasks" in command


@pytest.mark.asyncio
@pytest.mark.parametrize("packaged", [False, True])
async def test_detached_worker_environment_drops_database_secrets(
    monkeypatch, tmp_path, packaged
):
    _install_database_environment(monkeypatch)
    registry = _registry(queue=Queue.DEFAULT)
    _install_hostile_locale(monkeypatch, registry, packaged)
    runner_api = SimpleNamespace(
        server_url="https://simstack.example/api",
        runner_token="runner-secret",
        send_logs=AsyncMock(return_value={}),
        fail_task=AsyncMock(return_value={}),
        renew_lease=AsyncMock(return_value={}),
    )
    fake_context = SimpleNamespace(
        runner_api=runner_api,
        config=SimpleNamespace(
            resource="local",
            project_root=tmp_path,
            workdir=tmp_path / "work",
        ),
        runner_adapter=_execution_adapter(
            program={
                "local_worker_command": ["worker-wrapper", "configured-run-node"],
                "worker_network_guard_command": ["network-guard", "--"],
                "worker_environment_allowlist": [],
            },
        ),
    )
    monkeypatch.setattr(
        "simstack.core.services.node_execution_service.context", fake_context
    )
    monkeypatch.setattr("simstack.core.services.base_service.context", fake_context)
    process = _finished_process()
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "simstack.core.services.node_execution_service.asyncio.create_subprocess_exec",
        create_process,
    )
    service = NodeExecutionService(
        Resource(value="self"),
        interval=1,
        max_concurrent=1,
        shutdown_event=None,
    )

    assert (
        await service.run_node(
            registry,
            claim_id="claim-3",
            attempt=6,
            execution_api=runner_api,
        )
        is True
    )

    environment = create_process.await_args.kwargs["env"]
    _assert_database_environment_removed(environment)
    _assert_bootstrap_locale(environment, packaged)
    assert environment["SIMSTACK_RUNNER_TOKEN"] == "runner-secret"
    assert environment["SIMSTACK_TASK_CLAIM_ID"] == "claim-3"
    assert environment["SIMSTACK_RUNNER_WORKDIR"] == str(tmp_path / "work")
    assert create_process.await_args.args[:4] == (
        "network-guard",
        "--",
        "worker-wrapper",
        "configured-run-node",
    )
    assert create_process.await_args.kwargs["stdout"] is asyncio.subprocess.PIPE
    assert create_process.await_args.kwargs["stderr"] is asyncio.subprocess.PIPE


@pytest.mark.asyncio
@pytest.mark.parametrize("queue,in_docker", [(Queue.DEFAULT, False), (Queue.SLURM_QUEUE, False), (Queue.DEFAULT, True)])
@pytest.mark.parametrize("lease_failure", ["during-preparation", "before-dispatch"])
async def test_package_preparation_lease_failure_never_dispatches_any_worker(monkeypatch, tmp_path, queue, in_docker, lease_failure):
    registry = _registry(queue=queue)
    registry.parameters.in_docker = in_docker
    registry.workflow_revision = "a" * 64
    registry.workflow_package = "example.package"
    registry.workflow_installation_id = "installation-1"
    registry.runtime_profile = "isolated-python"
    runner_api = SimpleNamespace(
        send_logs=AsyncMock(return_value={}), fail_task=AsyncMock(return_value={}),
        renew_lease=AsyncMock(side_effect=RunnerApiError("Task claim changed", status_code=409)),
    )
    fake_context = SimpleNamespace(
        runner_api=runner_api, runner_adapter=MagicMock(),
        config=SimpleNamespace(resource="local", workdir=tmp_path),
    )
    monkeypatch.setattr("simstack.core.services.node_execution_service.context", fake_context)
    monkeypatch.setattr("simstack.core.services.base_service.context", fake_context)
    finished = asyncio.Event()

    async def preparation(*args, **kwargs):
        try:
            if lease_failure == "during-preparation":
                await asyncio.sleep(1)
        finally:
            finished.set()

    monkeypatch.setattr("simstack.runner_runtime.workflow_installation.ensure_claimed_package_runtime", preparation)
    launches = [AsyncMock(), AsyncMock(), AsyncMock()]
    for name, launch in zip(("asyncio.create_subprocess_exec", "submit_node", "run_docker"), launches):
        monkeypatch.setattr("simstack.core.services.node_execution_service." + name, launch)
    service = NodeExecutionService(Resource(value="self"), interval=1, max_concurrent=1, shutdown_event=None)
    service._lease_seconds = 0.03
    assert await service.run_node(registry, claim_id="claim-3", attempt=6, execution_api=runner_api) is False
    assert finished.is_set()
    assert runner_api.renew_lease.await_count == 1
    assert runner_api.fail_task.await_args.kwargs["error"] == "RunnerApiError"
    assert all(launch.await_count == 0 for launch in launches)


@pytest.mark.asyncio
async def test_detached_worker_failure_has_bounded_redacted_diagnostic(
    monkeypatch,
    tmp_path,
):
    registry = _registry(queue=Queue.DEFAULT)
    execution_token = "execution-token-must-not-leak"
    runner_api = SimpleNamespace(
        server_url="https://simstack.example/api",
        runner_token=execution_token,
        send_logs=AsyncMock(return_value={}),
        fail_task=AsyncMock(return_value={}),
        renew_lease=AsyncMock(return_value={}),
    )
    fake_context = SimpleNamespace(
        runner_api=runner_api,
        config=SimpleNamespace(
            resource="local",
            project_root=tmp_path,
            workdir=tmp_path / "work",
        ),
        runner_adapter=_execution_adapter(
            program={
                "local_worker_command": ["configured-run-node"],
                "worker_network_guard_command": ["network-guard", "--"],
                "worker_environment_allowlist": [],
            },
        ),
    )
    monkeypatch.setattr(
        "simstack.core.services.node_execution_service.context", fake_context
    )
    monkeypatch.setattr("simstack.core.services.base_service.context", fake_context)

    stdout = asyncio.StreamReader()
    stdout.feed_data(b"stdout startup marker\n")
    stdout.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_data((b"discarded line\n" * 2_000))
    stderr.feed_data(
        f"SIMSTACK_RUNNER_TOKEN={execution_token}\nTAIL STARTUP FAILURE\n".encode()
    )
    stderr.feed_eof()
    process = SimpleNamespace(
        pid=1234,
        stdout=stdout,
        stderr=stderr,
        returncode=1,
    )
    monkeypatch.setattr(
        "simstack.core.services.node_execution_service.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    service = NodeExecutionService(
        Resource(value="self"),
        interval=1,
        max_concurrent=1,
        shutdown_event=None,
    )

    assert (
        await service.run_node(
            registry,
            claim_id="claim-diagnostic",
            attempt=1,
            execution_api=runner_api,
        )
        is False
    )

    message = runner_api.fail_task.await_args.kwargs["message"]
    assert "TAIL STARTUP FAILURE" in message
    assert "stdout startup marker" in message
    assert "[earlier worker output truncated]" in message
    assert execution_token not in message
    assert "SIMSTACK_RUNNER_TOKEN=[REDACTED]" in message
    assert len(message.encode("utf-8")) < 20_000


@pytest.mark.asyncio
async def test_container_launch_contract_has_no_implicit_entrypoint(
    monkeypatch, tmp_path
):
    registry = _registry(queue=Queue.DEFAULT)
    registry.parameters.in_docker = True
    fake_context = SimpleNamespace(
        config=SimpleNamespace(
            resource="local", workdir=tmp_path / "work", project_root=tmp_path
        ),
        runner_adapter=_execution_adapter(
            program={
                "container_backend": "docker",
                "container_command": ["docker"],
                "container_image": "worker:latest",
                "container_project_root": "/source",
                "container_workdir": "/work",
            },
        ),
    )
    monkeypatch.setattr("simstack.core.run_docker.context", fake_context)

    with pytest.raises(RuntimeError, match="container_worker_command"):
        await run_docker(
            registry,
            claim_id="claim-closed",
            attempt=1,
            lease_seconds=120,
            runner_api=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_detached_launch_requires_explicit_worker_command(monkeypatch, tmp_path):
    registry = _registry(queue=Queue.DEFAULT)
    runner_api = SimpleNamespace(
        send_logs=AsyncMock(return_value={}),
        fail_task=AsyncMock(return_value={}),
    )
    fake_context = SimpleNamespace(
        runner_api=runner_api,
        config=SimpleNamespace(
            resource="local", project_root=tmp_path, workdir=tmp_path / "work"
        ),
        runner_adapter=_execution_adapter(),
    )
    monkeypatch.setattr(
        "simstack.core.services.node_execution_service.context", fake_context
    )
    monkeypatch.setattr("simstack.core.services.base_service.context", fake_context)
    service = NodeExecutionService(
        Resource(value="self"),
        interval=1,
        max_concurrent=1,
        shutdown_event=None,
    )

    assert (
        await service.run_node(
            registry,
            claim_id="claim-closed",
            attempt=1,
            execution_api=runner_api,
        )
        is False
    )
    runner_api.fail_task.assert_awaited_once()
    assert "local_worker_command" in runner_api.fail_task.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_claim_scoped_slurm_submission_cannot_fall_back_to_legacy(
    monkeypatch,
):
    registry = _registry(queue=Queue.SLURM_QUEUE)
    fake_context = SimpleNamespace(runner_mode=False)
    monkeypatch.setattr("simstack.core.submit_node.context", fake_context)

    with pytest.raises(
        RuntimeError,
        match="requires initialized API-only runner mode",
    ):
        await submit_node(
            registry,
            claim_id="claim-closed",
            attempt=1,
            runner_api=SimpleNamespace(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("packaged", [False, True])
async def test_slurm_script_requires_api_environment_and_updates_via_cas(
    monkeypatch, tmp_path, packaged
):
    _install_database_environment(monkeypatch)
    registry = _registry(queue=Queue.SLURM_QUEUE)
    _install_hostile_locale(monkeypatch, registry, packaged)
    runner_api = SimpleNamespace(
        server_url="https://simstack.example/api",
        runner_token="runner-secret",
        update_task_status=AsyncMock(return_value={}),
    )
    fake_context = SimpleNamespace(
        runner_mode=True,
        runner_api=runner_api,
        config=SimpleNamespace(
            resource="cluster",
            project_root=tmp_path,
            python_paths=[],
            workdir=tmp_path / "work",
            environment_start=None,
            docker=False,
        ),
        runner_adapter=_execution_adapter(
            program={"worker_environment_allowlist": []},
            setup={
                "slurm_worker_command": ["configured-run-node"],
                "worker_network_guard_command": ["network-guard", "--"],
                "slurm_submit_command": ["configured-sbatch"],
            },
        ),
    )
    monkeypatch.setattr("simstack.core.submit_node.context", fake_context)
    completed = SimpleNamespace(
        returncode=0,
        stdout="Submitted batch job 421\n",
        stderr="",
    )
    to_thread = AsyncMock(return_value=completed)
    monkeypatch.setattr("simstack.core.submit_node.asyncio.to_thread", to_thread)

    assert (
        await submit_node(
            registry,
            claim_id="claim-7",
            attempt=2,
            runner_api=runner_api,
        )
        is True
    )

    script_path = (
        runner_task_workdir(fake_context.config.workdir, str(registry.id))
        / "slurm_script.sh"
    )
    script = script_path.read_text(encoding="utf-8")
    assert "SIMSTACK_SERVER_URL is required" in script
    assert "SIMSTACK_RUNNER_TOKEN is required" in script
    assert 'SIMSTACK_TASK_CLAIM_ID="${SIMSTACK_TASK_CLAIM_ID}"' in script
    assert 'SIMSTACK_TASK_ATTEMPT="${SIMSTACK_TASK_ATTEMPT}"' in script
    assert "configured-run-node" in script
    assert "network-guard" in script
    assert "env -i" in script
    assert "SIMSTACK_RUNNER_DB_ENV_PURGE" not in script
    assert "unset" not in script
    assert _DATABASE_SECRET not in script
    assert "runner-secret" not in script
    assert to_thread.await_args.args[0] is subprocess.run
    environment = to_thread.await_args.kwargs["env"]
    _assert_database_environment_removed(environment)
    _assert_bootstrap_locale(environment, packaged)
    assert environment["SIMSTACK_RUNNER_TOKEN"] == "runner-secret"
    sbatch_command = to_thread.await_args.args[1]
    assert sbatch_command[0] == "configured-sbatch"
    export_arguments = [
        argument for argument in sbatch_command if argument.startswith("--export=")
    ]
    assert len(export_arguments) == 1
    assert "ALL" not in export_arguments[0].split("=", 1)[1].split(",")
    assert "SIMSTACK_RUNNER_TOKEN" in export_arguments[0]
    status_call = runner_api.update_task_status.await_args
    assert status_call.args == (str(registry.id),)
    assert status_call.kwargs["claim_id"] == "claim-7"
    assert status_call.kwargs["attempt"] == 2
    assert status_call.kwargs["expected_status"] == "slurm_queued"
    assert status_call.kwargs["status"] == "slurm_queued"
    assert status_call.kwargs["job_id"] == "421"
    assert status_call.kwargs["observed_at"].endswith("+00:00")
    queued_call = runner_api.update_task_status.await_args_list[0]
    assert queued_call.kwargs["expected_status"] == "retrieved"
    assert queued_call.kwargs["status"] == "slurm_queued"
    assert "job_id" not in queued_call.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "apptainer"])
async def test_slurm_container_exports_controls_and_allowlisted_values(
    monkeypatch, tmp_path, backend
):
    _install_database_environment(monkeypatch)
    monkeypatch.setenv("ProgramLicense", "allowed-license")
    registry = _registry(queue=Queue.SLURM_QUEUE)
    registry.parameters.in_docker = True
    runner_api = SimpleNamespace(
        server_url="https://simstack.example/api",
        runner_token="execution-secret",
        update_task_status=AsyncMock(return_value={}),
    )
    fake_context = SimpleNamespace(
        runner_mode=True,
        config=SimpleNamespace(
            resource="cluster", project_root=tmp_path, python_paths=[],
            workdir=tmp_path / "work", environment_start=None,
        ),
        runner_adapter=_execution_adapter(
            program={
                "worker_environment_allowlist": ["ProgramLicense"],
                "container_backend": backend,
                "container_command": ["configured-container"],
                "container_worker_command": ["configured-run-node"],
                "worker_network_guard_command": ["network-guard", "--"],
                "container_image": "configured-worker",
                "container_project_root": "/workspace/source",
                "container_workdir": "/workspace/tasks",
            },
            setup={
                "slurm_worker_command": ["configured-run-node"],
                "worker_network_guard_command": ["network-guard", "--"],
                "slurm_submit_command": ["configured-sbatch"],
            },
        ),
    )
    monkeypatch.setattr("simstack.core.submit_node.context", fake_context)
    submission = {}

    def fake_sbatch(command, **kwargs):
        # The state publication must be acknowledged before a fast worker can
        # read its task; this structurally removes the reverse handoff race.
        assert runner_api.update_task_status.await_count == 1
        submission.update(command=command, environment=kwargs["env"])
        return SimpleNamespace(
            returncode=0, stdout="Submitted batch job 421\n", stderr="",
        )

    monkeypatch.setattr("simstack.core.submit_node.subprocess.run", fake_sbatch)
    assert await submit_node(
        registry, claim_id="claim-container", attempt=1, runner_api=runner_api,
    )
    command = submission["command"]
    environment = submission["environment"]
    exported = command[-2].removeprefix("--export=").split(",")
    batch_environment = {name: environment[name] for name in exported}
    assert batch_environment["SIMSTACK_RUNNER_TOKEN"] == "execution-secret"
    assert batch_environment["ProgramLicense"] == "allowed-license"
    _assert_database_environment_removed(batch_environment)
    script = Path(command[-1]).read_text(encoding="utf-8")
    assert "execution-secret" not in script
    assert "allowed-license" not in script
    assert not any("execution-secret" in item for item in command)
    if backend == "apptainer":
        for name in ("SIMSTACK_RUNNER_TOKEN", "SIMSTACK_TASK_CLAIM_ID", "ProgramLicense"):
            assert batch_environment[f"APPTAINERENV_{name}"] == environment[name]
            assert f'APPTAINERENV_{name}="${{APPTAINERENV_{name}}}"' in script
        assert "APPTAINERENV_MONGODB_URI" not in exported
    else:
        assert not any(name.startswith("APPTAINERENV_") for name in exported)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_status",
    [TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING],
)
async def test_slurm_reconciliation_binds_active_attempt(monkeypatch, active_status):
    registry = _registry(queue=Queue.SLURM_QUEUE)
    registry.status = active_status
    registry.job_id = "421"
    execution_api = SimpleNamespace(
        renew_lease=AsyncMock(return_value={}),
        update_task_status=AsyncMock(return_value={}),
    )
    runner_api = SimpleNamespace(
        list_active_tasks=AsyncMock(
            return_value=[
                {
                    "claim_id": "claim-11",
                    "attempt": 9,
                    "lease_until": "2026-08-17T12:00:00Z",
                    "execution_token": "execution-token-11",
                    "execution_token_expires_at": "2026-08-17T12:05:00Z",
                    "task": _task_bundle(registry),
                }
            ]
        ),
        execution_client=MagicMock(return_value=execution_api),
    )
    fake_context = SimpleNamespace(runner_api=runner_api)
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.context", fake_context
    )
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.get_job_info",
        lambda *_args: None,
    )
    service = SlurmStatusService(Resource(value="self"), interval=30, max_concurrent=2)

    await service.execute()

    runner_api.execution_client.assert_called_once_with("execution-token-11")
    execution_api.renew_lease.assert_awaited_once_with(
        str(registry.id),
        claim_id="claim-11",
        attempt=9,
        lease_seconds=120,
    )
    status_call = execution_api.update_task_status.await_args
    assert status_call.args == (str(registry.id),)
    assert status_call.kwargs["claim_id"] == "claim-11"
    assert status_call.kwargs["attempt"] == 9
    assert status_call.kwargs["expected_status"] == active_status.value
    assert status_call.kwargs["status"] == "timeout"
    assert status_call.kwargs["observed_at"].endswith("+00:00")
    assert status_call.kwargs["message"] == "Slurm job is no longer present in squeue"


def test_slurm_status_uses_placeholder_argv_and_strict_json(monkeypatch, tmp_path):
    registry = _registry(queue=Queue.SLURM_QUEUE)
    fake_context = SimpleNamespace(
        runner_adapter=_execution_adapter(
            setup={
                "slurm_status_command": [
                    "scheduler-adapter",
                    "status",
                    "{job_id}",
                ]
            }
        )
    )
    monkeypatch.setattr("simstack.runner_runtime.slurm.context", fake_context)
    monkeypatch.setattr(
        "simstack.runner_runtime.environment.sanitized_trusted_helper_environment",
        lambda: {"PATH": os.environ["PATH"]},
    )
    completed = SimpleNamespace(
        returncode=0,
        stdout=(
            '{"present":true,"name":"node","user":"runner",'
            '"code":"R","time":"00:02","nodes":["n1","n2"]}'
        ),
        stderr="",
    )
    run = MagicMock(return_value=completed)
    monkeypatch.setattr("simstack.runner_runtime.slurm.subprocess.run", run)

    info = get_job_info("421", registry.id, Resource(value="cluster"))

    assert info is not None
    assert info.nodes == ["n1", "n2"]
    assert run.call_args.args[0] == ["scheduler-adapter", "status", "421"]
    assert "shell" not in run.call_args.kwargs


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (["scheduler-adapter", "status"], "exactly one"),
        (
            ["scheduler-adapter", "{job_id}", "{job_id}"],
            "exactly one",
        ),
    ],
)
def test_slurm_status_requires_single_job_placeholder(monkeypatch, command, message):
    monkeypatch.setattr(
        "simstack.runner_runtime.slurm.context",
        SimpleNamespace(
            runner_adapter=_execution_adapter(setup={"slurm_status_command": command})
        ),
    )

    with pytest.raises(RuntimeError, match=message):
        run_squeue_for_job("421")


def test_slurm_status_rejects_legacy_whitespace_output(monkeypatch):
    monkeypatch.setattr(
        "simstack.runner_runtime.slurm.context",
        SimpleNamespace(
            runner_adapter=_execution_adapter(
                setup={"slurm_status_command": ["scheduler-adapter", "{job_id}"]}
            )
        ),
    )
    monkeypatch.setattr(
        "simstack.runner_runtime.environment.sanitized_trusted_helper_environment",
        lambda: {},
    )
    monkeypatch.setattr(
        "simstack.runner_runtime.slurm.subprocess.run",
        MagicMock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout="JOBID PARTITION NAME USER ST TIME NODES NODELIST\n",
                stderr="",
            )
        ),
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        run_squeue_for_job("421")


def test_slurm_status_rejects_non_numeric_job_id_before_subprocess(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("simstack.runner_runtime.slurm.subprocess.run", run)

    with pytest.raises(ValueError, match="ASCII digits"):
        run_squeue_for_job("421; curl attacker")

    run.assert_not_called()


@pytest.mark.asyncio
async def test_slurm_reconciliation_is_bounded_and_concurrent(monkeypatch):
    registries = []
    active_tasks = []
    execution_apis = {}
    for index in range(4):
        registry = _registry(queue=Queue.SLURM_QUEUE)
        registry.status = TaskStatus.SLURM_RUNNING
        registry.job_id = str(500 + index)
        registries.append(registry)
        token = f"execution-token-{index}"
        active_tasks.append(
            {
                "claim_id": f"claim-{index}",
                "attempt": index + 1,
                "lease_until": "2026-08-17T12:00:00Z",
                "execution_token": token,
                "execution_token_expires_at": "2026-08-17T12:05:00Z",
                "task": _task_bundle(registry),
            }
        )
        execution_apis[token] = SimpleNamespace(
            renew_lease=AsyncMock(return_value={}),
            update_task_status=AsyncMock(return_value={}),
        )
    runner_api = SimpleNamespace(
        list_active_tasks=AsyncMock(return_value=active_tasks),
        execution_client=lambda token: execution_apis[token],
    )
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.context",
        SimpleNamespace(runner_api=runner_api),
    )
    lock = threading.Lock()
    active = 0
    maximum = 0

    def slow_status(*_args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return None

    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.get_job_info", slow_status
    )
    service = SlurmStatusService(Resource(value="self"), interval=30, max_concurrent=2)

    await service.execute()

    assert maximum == 2
    assert all(api.renew_lease.await_count == 1 for api in execution_apis.values())
    assert all(
        api.update_task_status.await_count == 1 for api in execution_apis.values()
    )


def test_runner_pid_path_hashes_malicious_opaque_resource(tmp_path):
    malicious_resource = "../../outside\ncontrol"

    pid_file = runner_pid_file(tmp_path, malicious_resource)

    assert pid_file.parent == tmp_path.resolve()
    assert malicious_resource not in pid_file.name
    assert pid_file.name.startswith("runner-")
    assert pid_file.suffix == ".pid"


def test_runner_pid_file_is_written_private(monkeypatch, tmp_path):
    manager = object.__new__(RunnerManager)
    manager._pid = 4321
    manager._pid_file = runner_pid_file(tmp_path, "resource")

    manager._write_pid_file()

    assert manager._pid_file.read_text(encoding="ascii") == "4321"
    assert stat.S_IMODE(manager._pid_file.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_container_failure_output_is_bounded_and_execution_token_redacted(monkeypatch):
    registry = _registry(queue=Queue.DEFAULT)
    api = SimpleNamespace(runner_token="execution-token-do-not-persist")
    monkeypatch.setattr(
        "simstack.core.run_docker.context",
        SimpleNamespace(config=object(), runner_adapter=object()),
    )
    monkeypatch.setattr(
        "simstack.core.run_docker.container_launch",
        lambda *args, **kwargs: (["configured-container"], {}),
    )
    process = _finished_process(
        returncode=1,
        stderr=b"discarded line\n" * 10000 + (
            f"Authorization: Bearer {api.runner_token}\nCONTAINER FAILURE TAIL\n".encode()
        ),
    )
    monkeypatch.setattr(
        "simstack.core.run_docker.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    with pytest.raises(RuntimeError) as failure:
        await run_docker(
            registry, claim_id="claimed", attempt=1, lease_seconds=120, runner_api=api,
        )
    message = str(failure.value)
    assert "CONTAINER FAILURE TAIL" in message
    assert "[earlier worker output truncated]" in message
    assert "Authorization: Bearer [REDACTED]" in message
    assert api.runner_token not in message
    assert len(message.encode()) < 20000
