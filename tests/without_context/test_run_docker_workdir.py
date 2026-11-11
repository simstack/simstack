import logging
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from simstack.core.run_docker import (
    CONTAINER_WORKDIR,
    LEGACY_CONTAINER_WORKDIR,
    DockerRunResult,
    container_resource_args,
    docker_cidfile_path,
    docker_cpu_limit,
    docker_memory_limit,
    ensure_host_task_workdir,
    format_container_failure_error,
    inspect_docker_oomkilled,
    prepare_docker_cidfile,
    run_docker,
    run_docker_with_outcome,
    _docker_program_config,
)
from simstack.core.definitions import TaskStatus
from simstack.core.run_node_protocol import RunNodeResult, encode_run_node_result
from simstack.util.resource_config import ResourceConfig


_SUCCESS_PROTOCOL = encode_run_node_result(RunNodeResult(True, "bool")).encode()


@pytest.fixture(autouse=True)
def _runner_owner_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    monkeypatch.setattr("simstack.runner_runtime.launch_claims.record_spawn", lambda *args: None)
    monkeypatch.setattr("simstack.runner_runtime.launch_claims.record_container_launch", lambda *args, **kwargs: False)
    monkeypatch.setattr("simstack.runner_runtime.launch_claims.receipt", lambda *args: None)
    monkeypatch.setattr("simstack.core.run_docker.docker_daemon_identity", lambda: "test-daemon")
    def observe(name):
        from simstack.core.run_docker import context
        return {"Id": "cid123", "State": context.server.tasks.container_state,
            "Config": {"Labels": {"simstack.execution": name}}}
    monkeypatch.setattr("simstack.core.run_docker.inspect_docker_execution", observe)
    monkeypatch.setattr(
        "simstack.core.run_docker.launcher_owner_ids",
        lambda: (1000, 1000),
    )


def test_ensure_host_task_workdir_creates_node_and_task_dirs(tmp_path: Path):
    task_dir = ensure_host_task_workdir(tmp_path, "psi4_calculator", "abc123")

    assert task_dir == tmp_path / "psi4_calculator" / "abc123"
    assert task_dir.is_dir()
    assert task_dir.parent.is_dir()


def test_ensure_host_task_workdir_is_idempotent(tmp_path: Path):
    first = ensure_host_task_workdir(tmp_path, "psi4_calculator", "abc123")
    second = ensure_host_task_workdir(tmp_path, "psi4_calculator", "abc123")

    assert first == second
    assert second.is_dir()


def test_docker_cpu_limit_is_cpus_per_task_times_tasks():
    slurm = SimpleNamespace(cpus_per_task=4, tasks=2, tasks_per_node=None)
    assert docker_cpu_limit(slurm) == 8


def test_docker_cpu_limit_falls_back_to_tasks_per_node():
    slurm = SimpleNamespace(cpus_per_task=3, tasks=None, tasks_per_node=2)
    assert docker_cpu_limit(slurm) == 6


def test_docker_cpu_limit_omitted_when_unset():
    assert docker_cpu_limit(None) is None
    assert docker_cpu_limit(SimpleNamespace()) is None


def test_docker_memory_limit_uses_mem_and_mem_per_cpu():
    assert docker_memory_limit(SimpleNamespace(mem="8G")) == "8g"
    assert docker_memory_limit(SimpleNamespace(mem="8G"), uppercase=True) == "8G"
    assert (
        docker_memory_limit(SimpleNamespace(mem_per_cpu="2G", cpus_per_task=4, tasks=2))
        == "16g"
    )


def test_container_resource_args_for_docker_and_apptainer():
    slurm = SimpleNamespace(cpus_per_task=4, tasks=2, mem="8G")
    assert container_resource_args("docker", slurm) == ["--cpus", "8", "--memory", "8g"]
    assert container_resource_args("apptainer", slurm) == []
    assert container_resource_args("docker", None) == []


def _registry_entry(
    name: str = "psi4_calculator",
    resource: str = "local",
    slurm_parameters=None,
):
    return SimpleNamespace(
        id="abc123",
        name=name,
        parameters=SimpleNamespace(resource=resource, queue=None, slurm_parameters=slurm_parameters),
        status=TaskStatus.RETRIEVED,
        error=None,
        return_kind=None,
        _execution_claim_id="original-claim",
        _execution_generation="original-generation",
    )


class _DomainAPI:
    app_status = TaskStatus.COMPLETED

    def __init__(self):
        self.fail_attempt = AsyncMock(side_effect=self._fail)
        self.record_process = AsyncMock()
        self.admit_container = AsyncMock()
        self.termination_confirmed = AsyncMock()
        self.reconcile_attempts = AsyncMock(return_value={"claims": [], "outcomes": []})
        self.container_state = {"Status": "exited", "ExitCode": 0}

    def execution(self, entry):
        return {"task_id": str(entry.id), "claim_id": entry._execution_claim_id,
            "generation": entry._execution_generation, "start_id": entry._execution_start_id}

    async def get_outcome(self, entry):
        return SimpleNamespace(**{**vars(entry), "status": self.app_status, "return_kind": None})

    async def children(self, entry):
        return []

    async def _fail(self, entry, error, **kwargs):
        entry.status = TaskStatus.FAILED
        entry.error = entry.error or error
        return entry


def _mock_context(tmp_path: Path, resource_config: ResourceConfig):
    (tmp_path / "simstack.toml").write_text("", encoding="utf-8")
    return SimpleNamespace(
        resource_config=resource_config,
        config=SimpleNamespace(resource="local", workdir=tmp_path, project_root=tmp_path),
        server=SimpleNamespace(tasks=_DomainAPI(), requests=SimpleNamespace(
            server_url="https://server.example/api", authorization="runner", resource="local",
            bootstrap_data={"account_id": "unit-test-account"})),
    )


def test_docker_program_config_maps_self_to_current_resource(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[cluster.program.psi4_calculator]\ndocker_image = "cluster-image"\n'
    )
    resource_config = ResourceConfig(tmp_path, "cluster")
    mock_context = _mock_context(tmp_path, resource_config)
    mock_context.config.resource = "cluster"

    with patch("simstack.core.run_docker.context", mock_context):
        program, resource = _docker_program_config(
            _registry_entry(resource="self")
        )

    assert resource == "cluster"
    assert program["docker_image"] == "cluster-image"


def test_docker_program_config_does_not_fallback_for_explicit_resource(
    tmp_path: Path,
):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)

    with patch("simstack.core.run_docker.context", mock_context):
        program, resource = _docker_program_config(
            _registry_entry(resource="remote")
        )

    assert resource == "remote"
    assert program == {}


@pytest.mark.asyncio
async def test_run_docker_does_not_fallback_to_context_registry(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[local]\ndocker_registry = "local.registry"\n'
        '[remote.program.psi4_calculator]\ndocker_image = "remote-image"\n'
    )
    resource_config = ResourceConfig(tmp_path, "local")
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry(resource="remote")
    proc = AsyncMock(pid=4242)
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch(
            "simstack.core.run_docker.pull_docker_image", AsyncMock()
        ) as mock_pull,
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
    ):
        assert await run_docker(registry_entry) is True

    mock_pull.assert_awaited_once_with("remote-image", None)


@pytest.mark.asyncio
async def test_run_docker_reloads_config_before_image_lookup(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[local.program.psi4_calculator]\nrun_command = \"psi4\"\n")
    resource_config = ResourceConfig(tmp_path, "local")
    assert "docker_image" not in resource_config.get_program("psi4_calculator")

    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
    )

    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    proc = AsyncMock(pid=4242)
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
    ):
        result = await run_docker(registry_entry)

    assert result is True
    cmd = mock_exec.await_args_list[0].args
    assert "molecular-qm-psi4:latest" in cmd
    assert "--cpus" not in cmd
    assert "--memory" not in cmd
    assert resource_config.get_program("psi4_calculator")["docker_image"] == (
        "molecular-qm-psi4:latest"
    )


@pytest.mark.asyncio
async def test_run_docker_applies_slurm_cpu_and_memory_limits(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
    )
    resource_config = ResourceConfig(tmp_path, "local")
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry(
        slurm_parameters=SimpleNamespace(cpus_per_task=4, tasks=2, mem="8G")
    )

    proc = AsyncMock(pid=4242)
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
    ):
        result = await run_docker(registry_entry)

    assert result is True
    cmd = list(mock_exec.await_args_list[0].args)
    image_index = cmd.index("molecular-qm-psi4:latest")
    assert cmd[:image_index][cmd[:image_index].index("--cpus") + 1] == "8"
    assert cmd[:image_index][cmd[:image_index].index("--memory") + 1] == "8g"
    assert cmd.index("--cpus") < image_index
    assert cmd.index("--memory") < image_index


@pytest.mark.asyncio
async def test_run_docker_apptainer_omits_cpu_and_memory_flags(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
        'docker_cmd = "apptainer"\n'
    )
    resource_config = ResourceConfig(tmp_path, "local")
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry(
        slurm_parameters=SimpleNamespace(cpus_per_task=4, tasks=2, mem="8G")
    )

    proc = AsyncMock(pid=4242)
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
    ):
        result = await run_docker(registry_entry)

    assert result is True
    command = list(mock_exec.await_args.args)
    assert command[:2] == ["apptainer", "run"]
    assert "--cpus" not in command
    assert "--memory" not in command
    assert mock_exec.await_args.kwargs["env"]["APPTAINERENV_SIMSTACK_CONTAINER_BACKEND"] == "apptainer"
    assert mock_context.server.tasks.record_process.await_args.kwargs["container_backend"] == "apptainer"


@pytest.mark.asyncio
async def test_run_docker_fails_when_reloaded_config_has_no_image(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
    )
    resource_config = ResourceConfig(tmp_path, "local")

    config_file.write_text("[local.program.psi4_calculator]\nrun_command = \"psi4\"\n")

    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    with patch("simstack.core.run_docker.context", mock_context):
        result = await run_docker(registry_entry)

    assert result is False
    assert registry_entry.status == TaskStatus.FAILED
    mock_context.server.tasks.fail_attempt.assert_awaited()
    assert "docker_image" not in resource_config.get_program("psi4_calculator")


def test_format_container_failure_error_oomkilled():
    msg = format_container_failure_error(137, oom_killed=True)
    assert "exit 137" in msg
    assert "OOMKilled=true" in msg
    assert "out of memory" in msg.lower()
    assert "mem/mem_per_cpu unset" in msg


def test_format_container_failure_error_sigkill_without_oom_flag():
    msg = format_container_failure_error(137, oom_killed=False)
    assert "exit 137" in msg
    assert "OOMKilled=false" in msg
    assert "Likely OOM / SIGKILL" in msg


def test_format_container_failure_error_generic_exit():
    msg = format_container_failure_error(1)
    assert "exit 1" in msg
    assert "out of memory" not in msg.lower()
    assert "Likely OOM" not in msg
    assert "SIGKILL" not in msg


def test_format_container_failure_error_includes_memory_limit():
    msg = format_container_failure_error(137, oom_killed=True, memory_limit="8g")
    assert "docker --memory was 8g" in msg
    assert "unset" not in msg


def test_prepare_docker_cidfile_deletes_existing_file(tmp_path: Path):
    cidfile = docker_cidfile_path(tmp_path)
    cidfile.write_text("stale\n", encoding="utf-8")
    prepared = prepare_docker_cidfile(tmp_path)
    assert prepared == cidfile
    assert not prepared.exists()


def _psi4_image_config(tmp_path: Path) -> ResourceConfig:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
    )
    return ResourceConfig(tmp_path, "local")


def _fake_docker_exec(*, returncode: int = 0, cid: str = "cid123"):
    async def _exec(*cmd, **kwargs):
        cmd_list = list(cmd)
        if "--cidfile" in cmd_list:
            cidfile = Path(cmd_list[cmd_list.index("--cidfile") + 1])
            cidfile.parent.mkdir(parents=True, exist_ok=True)
            cidfile.write_text(cid + "\n", encoding="utf-8")
        from simstack.core import run_docker as module
        module.context.server.tasks.app_status = TaskStatus.COMPLETED if returncode == 0 else TaskStatus.RUNNING
        module.context.server.tasks.container_state = {"Status": "exited", "ExitCode": returncode}
        proc = AsyncMock(pid=4242)
        proc.returncode = returncode
        stdout = _SUCCESS_PROTOCOL if returncode == 0 else b""
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        return proc

    return _exec


@pytest.mark.asyncio
@pytest.mark.parametrize("command,returncode,expected_exit,expected_signal", [
    ("docker", 17, 17, None), ("docker", 137, 137, None), ("apptainer", -9, None, 9),
])
async def test_container_early_failure_preserves_observed_exit_signal_in_api_and_journal(
    tmp_path, monkeypatch, command, returncode, expected_exit, expected_signal,
):
    from simstack.runner_runtime.pending_execution_failures import pending_failures
    config = tmp_path / "config.toml"
    config.write_text('[local.program.psi4_calculator]\ndocker_image="probe-image"\ndocker_cmd="' + command + '"\n')
    active = _mock_context(tmp_path, ResourceConfig(config, "local"))
    from simstack.server_api.http_transport import RunnerAPIError
    active.server.tasks.reconcile_attempts.side_effect = RunnerAPIError("offline", status_code=503)
    entry = _registry_entry()
    captured = []
    execute = _fake_docker_exec(returncode=returncode)
    async def spawn(*args, **kwargs):
        captured.append((args, kwargs))
        return await execute(*args, **kwargs)
    monkeypatch.setattr("simstack.core.run_docker.context", active)
    monkeypatch.setattr("simstack.core.run_docker.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("simstack.core.run_docker.inspect_docker_oomkilled", lambda _: False)
    assert not (await run_docker_with_outcome(entry)).success
    failure = active.server.tasks.fail_attempt.await_args
    assert failure.args[0] is entry
    assert failure.kwargs["exit_code"] == expected_exit and failure.kwargs["signal"] == expected_signal
    records = list(pending_failures())
    assert len(records) == 1
    assert records[0][1]["exit_code"] == expected_exit and records[0][1]["signal"] == expected_signal
    assert records[0][1]["actor"]["account_id"] == "unit-test-account"
    assert captured[0][1]["env"]["SIMSTACK_EXECUTION_ACCOUNT_ID"] == "unit-test-account"
    if command == "docker":
        assert "SIMSTACK_EXECUTION_ACCOUNT_ID" in captured[0][0]
    else:
        assert captured[0][1]["env"]["APPTAINERENV_SIMSTACK_EXECUTION_ACCOUNT_ID"] == "unit-test-account"


@pytest.mark.asyncio
async def test_run_docker_passes_cidfile(tmp_path: Path):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_docker_exec(),
        ) as mock_exec,
    ):
        result = await run_docker(registry_entry)

    assert result is True
    cmd = list(mock_exec.await_args_list[0].args)
    assert "--cidfile" in cmd
    cidfile = Path(cmd[cmd.index("--cidfile") + 1])
    expected = tmp_path / "psi4_calculator" / "abc123" / ".docker_cid"
    assert cidfile == expected
    assert f"{tmp_path}:{CONTAINER_WORKDIR}" in cmd
    assert f"{tmp_path}:{LEGACY_CONTAINER_WORKDIR}" in cmd
    assert CONTAINER_WORKDIR == "/mnt"
    assert LEGACY_CONTAINER_WORKDIR == "/tmp/simstack"
    assert not CONTAINER_WORKDIR.startswith("/tmp")
    assert not CONTAINER_WORKDIR.startswith("/root/")


@pytest.mark.asyncio
async def test_run_docker_oomkilled_sets_error(tmp_path: Path):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_docker_exec(returncode=137),
        ),
        patch("simstack.core.run_docker.inspect_docker_oomkilled", return_value=True),
    ):
        result = await run_docker(registry_entry)

    assert result is False
    assert registry_entry.status == TaskStatus.FAILED
    assert registry_entry.error is not None
    assert "exit 137" in registry_entry.error
    assert "OOMKilled=true" in registry_entry.error
    assert "out of memory" in registry_entry.error.lower()
    assert "mem/mem_per_cpu unset" in registry_entry.error


@pytest.mark.asyncio
async def test_run_docker_sigkill_without_oomkilled_sets_error(tmp_path: Path):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_docker_exec(returncode=137),
        ),
        patch("simstack.core.run_docker.inspect_docker_oomkilled", return_value=False),
    ):
        result = await run_docker(registry_entry)

    assert result is False
    assert registry_entry.status == TaskStatus.FAILED
    assert registry_entry.error is not None
    assert "exit 137" in registry_entry.error
    assert "OOMKilled=false" in registry_entry.error
    assert "Likely OOM / SIGKILL" in registry_entry.error


@pytest.mark.asyncio
async def test_run_docker_generic_failure_is_not_oom(tmp_path: Path, caplog):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_docker_exec(returncode=1),
        ),
        patch("simstack.core.run_docker.inspect_docker_oomkilled", return_value=False),
        caplog.at_level(logging.ERROR, logger="DockerRunner"),
    ):
        result = await run_docker(registry_entry)

    assert result is False
    assert registry_entry.status == TaskStatus.FAILED
    assert registry_entry.error is not None
    assert "exit 1" in registry_entry.error
    assert "out of memory" not in registry_entry.error.lower()
    assert "Likely OOM" not in registry_entry.error
    assert "SIGKILL" not in registry_entry.error
    assert "task_id: abc123" in caplog.text
    assert "task_id=abc123" not in caplog.text


def test_inspect_docker_oomkilled_parses_true_false():
    with patch("simstack.core.run_docker.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="true\n")
        assert inspect_docker_oomkilled("cid") is True
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="false\n")
        assert inspect_docker_oomkilled("cid") is False
        mock_run.return_value = SimpleNamespace(returncode=1, stdout="")
        assert inspect_docker_oomkilled("cid") is None


def _bind_targets(command: list[str]) -> list[str]:
    return [command[index + 1] for index, arg in enumerate(command[:-1]) if arg == "--bind"]


@pytest.mark.asyncio
async def test_run_docker_apptainer_binds_job_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scratch = tmp_path / "job-scratch"
    scratch.mkdir()
    monkeypatch.setenv("SCRATCH", str(scratch))
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
        'docker_cmd = "apptainer"\n'
    )
    resource_config = ResourceConfig(tmp_path, "local")
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()
    proc = AsyncMock()
    proc.pid = 4242
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
    ):
        result = await run_docker(registry_entry)

    assert result is True
    targets = _bind_targets(list(mock_exec.await_args.args))
    assert f"{scratch}:{scratch}" in targets
    assert f"{tmp_path}:{CONTAINER_WORKDIR}" in targets


@pytest.mark.asyncio
@pytest.mark.parametrize("scratch_root", ["/tmp", "/scratch", "/ramdisk"])
async def test_run_docker_apptainer_does_not_bind_scratch_mount_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_root: str
):
    monkeypatch.setenv("SCRATCH", scratch_root)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
        'docker_cmd = "apptainer"\n'
    )
    resource_config = ResourceConfig(tmp_path, "local")
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()
    proc = AsyncMock()
    proc.pid = 4242
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
        patch.object(Path, "is_dir", return_value=True),
    ):
        result = await run_docker(registry_entry)

    assert result is True
    scratch_path = Path(scratch_root)
    assert f"{scratch_path}:{scratch_path}" not in _bind_targets(list(mock_exec.await_args.args))


@pytest.mark.asyncio
async def test_run_docker_apptainer_sigkill_uses_exit_code_heuristic(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[local.program.psi4_calculator]\n"
        'docker_image = "molecular-qm-psi4:latest"\n'
        'docker_cmd = "apptainer"\n'
    )
    resource_config = ResourceConfig(tmp_path, "local")
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()
    mock_context.server.tasks.app_status = TaskStatus.RUNNING

    proc = AsyncMock(pid=4242)
    proc.returncode = 137
    proc.communicate = AsyncMock(return_value=(b"", b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ) as mock_exec,
        patch("simstack.core.run_docker.inspect_docker_oomkilled") as mock_inspect,
    ):
        result = await run_docker(registry_entry)

    assert result is False
    mock_inspect.assert_not_called()
    assert registry_entry.error is not None
    assert "exit 137" in registry_entry.error
    assert "Likely OOM / SIGKILL" in registry_entry.error
    assert "OOMKilled" not in registry_entry.error
    command = list(mock_exec.await_args.args)
    assert f"{tmp_path}:{CONTAINER_WORKDIR}" in command
    assert f"{tmp_path}:{LEGACY_CONTAINER_WORKDIR}" in command
    assert CONTAINER_WORKDIR == "/mnt"
    assert command[command.index("--no-mount") + 1] == "tmp"
    assert f"{tmp_path}:/tmp" not in command
    assert not any("site-packages/simstack" in argument for argument in command)


@pytest.mark.asyncio
async def test_run_docker_redacts_and_bounds_database_uri_in_child_errors(
    tmp_path: Path, caplog
):
    secret = "mongodb://docker-user:docker-password@db.internal:27017/simstack"
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    mock_context.server.tasks.app_status = TaskStatus.RUNNING
    mock_context.server.tasks.container_state["ExitCode"] = 1
    registry_entry = _registry_entry()

    proc = AsyncMock(pid=4242)
    proc.returncode = 1
    proc.communicate = AsyncMock(
        return_value=(
            ("x" * 5000 + secret).encode(),
            ("y" * 5000 + secret).encode(),
        )
    )

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch("simstack.core.run_docker.inspect_docker_oomkilled", return_value=False),
    ):
        result = await run_docker(registry_entry)

    assert result is False
    assert secret not in caplog.text
    assert "docker-user" not in caplog.text
    assert "docker-password" not in caplog.text
    assert secret not in (registry_entry.error or "")
    assert len(registry_entry.error or "") <= 4096


@pytest.mark.asyncio
async def test_run_docker_preserves_child_return_kind(tmp_path: Path):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()
    proc = AsyncMock(pid=4242)
    proc.returncode = 0
    proc.communicate = AsyncMock(
        return_value=(
            encode_run_node_result(RunNodeResult(True, "multiple")).encode(),
            b"",
        )
    )

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
    ):
        result = await run_docker_with_outcome(registry_entry)

    assert result == DockerRunResult(True, "multiple")


@pytest.mark.asyncio
async def test_run_docker_failure_preserves_child_persisted_metadata(tmp_path: Path):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()
    child_entry = SimpleNamespace(
        id=registry_entry.id,
        status=TaskStatus.FAILED,
        error="child error",
        info_files=["child-log"],
        return_kind="exception",
        message="child message",
    )
    mutation = AsyncMock()
    mock_context.server.tasks.get_outcome = AsyncMock(return_value=child_entry)
    mock_context.server.tasks.fail_attempt = mutation
    proc = AsyncMock(pid=4242)
    proc.returncode = 1
    proc.communicate = AsyncMock(
        return_value=(
            encode_run_node_result(
                RunNodeResult(False, "exception", "different protocol error")
            ).encode(),
            b"container stderr",
        )
    )

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch("simstack.core.run_docker.inspect_docker_oomkilled", return_value=False),
    ):
        result = await run_docker_with_outcome(registry_entry)

    assert result.success is False
    assert result.error == "child error"
    assert child_entry.info_files == ["child-log"]
    assert child_entry.return_kind == "exception"
    assert child_entry.message == "child message"
    assert mutation.await_args.args[0] is registry_entry
    assert mutation.await_args.args[1] == "child error"
    assert child_entry.error == "child error"


@pytest.mark.asyncio
async def test_run_docker_redacts_database_uri_from_spawn_exception(
    tmp_path: Path, caplog
):
    secret = "mongodb://docker-user:docker-password@db.internal:27017/simstack"
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    mock_context.server.tasks.app_status = TaskStatus.RUNNING
    registry_entry = _registry_entry()

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError(f"spawn failed with {secret}")),
        ),
    ):
        result = await run_docker(registry_entry)

    assert result is False
    assert secret not in caplog.text
    assert "docker-user" not in caplog.text
    assert "docker-password" not in caplog.text
    assert secret not in (registry_entry.error or "")


def _harmonize_aware_exec(*, job_returncode: int):
    async def _exec(*cmd, **kwargs):
        cmd_list = list(cmd)
        if "--user" in cmd_list and "0:0" in cmd_list:
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc
        return await _fake_docker_exec(returncode=job_returncode)(*cmd, **kwargs)

    return _exec


def _harmonize_command(mock_exec) -> list[str]:
    commands = [list(call.args) for call in mock_exec.await_args_list]
    harmonize = [cmd for cmd in commands if "--user" in cmd and "0:0" in cmd]
    assert len(harmonize) == 1
    return harmonize[0]


@pytest.mark.asyncio
async def test_run_docker_harmonizes_task_dir_after_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "simstack.core.run_docker.launcher_owner_ids",
        lambda: (4242, 4243),
    )
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("simstack.core.run_docker.platform.system", return_value="Linux"),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_harmonize_aware_exec(job_returncode=0),
        ) as mock_exec,
    ):
        result = await run_docker_with_outcome(registry_entry)

    assert result.success is True
    command = _harmonize_command(mock_exec)
    task_dir = tmp_path / "psi4_calculator" / "abc123"
    assert command[:8] == [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--entrypoint",
    ]
    assert command[8] == "python"
    assert f"{task_dir.resolve()}:/work" in command
    assert "molecular-qm-psi4:latest" in command
    assert command[-3:] == ["4242", "4243", "/work"]
    assert "0o664" in command[command.index("-c") + 1]
    assert "0o775" in command[command.index("-c") + 1]


@pytest.mark.asyncio
async def test_run_docker_harmonizes_task_dir_after_nonzero_exit(tmp_path: Path):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("simstack.core.run_docker.platform.system", return_value="Linux"),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_harmonize_aware_exec(job_returncode=1),
        ) as mock_exec,
        patch("simstack.core.run_docker.inspect_docker_oomkilled", return_value=False),
    ):
        result = await run_docker_with_outcome(registry_entry)

    assert result.success is False
    assert registry_entry.error is not None
    assert "exit 1" in registry_entry.error
    command = _harmonize_command(mock_exec)
    assert command[-3:] == ["1000", "1000", "/work"]
    assert "--user" in command
    assert "0:0" in command


@pytest.mark.asyncio
async def test_run_docker_skips_harmonize_on_windows(tmp_path: Path):
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    registry_entry = _registry_entry()

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("simstack.core.run_docker.platform.system", return_value="Windows"),
        patch("simstack.core.run_docker.launcher_owner_ids", side_effect=AssertionError("should not be called")),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_docker_exec(returncode=0),
        ) as mock_exec,
    ):
        result = await run_docker_with_outcome(registry_entry)

    assert result.success is True
    commands = [list(call.args) for call in mock_exec.await_args_list]
    assert not any("--network" in cmd for cmd in commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 17])
async def test_committed_result_survives_launcher_exit_and_cleanup_failure(tmp_path, returncode):
    mock_context = _mock_context(tmp_path, _psi4_image_config(tmp_path))
    mock_context.server.tasks.container_state = {"Status": "exited", "ExitCode": returncode}
    entry = _registry_entry()
    committed = SimpleNamespace(**{**vars(entry), "status": TaskStatus.COMPLETED,
        "return_kind": "multiple", "error": None})
    mock_context.server.tasks.get_outcome = AsyncMock(return_value=committed)
    process = AsyncMock(pid=4242, returncode=returncode)
    process.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b"launcher diagnostic"))
    with patch("simstack.core.run_docker.context", mock_context), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
         patch("simstack.core.run_docker._harmonize_docker_task_dir", AsyncMock(side_effect=PermissionError("denied"))):
        result = await run_docker_with_outcome(entry)
    assert result == DockerRunResult(True, "multiple")
    mock_context.server.tasks.fail_attempt.assert_not_awaited()
    if returncode:
        assert mock_context.server.tasks.reconcile_attempts.await_args.kwargs["outcomes"][0]["exit_code"] == returncode
    else:
        mock_context.server.tasks.reconcile_attempts.assert_not_awaited()
    assert committed.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_container_receives_generation_and_shared_failure_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMSTACK_EXECUTION_GENERATION", "original-generation")
    mock_context = _mock_context(tmp_path, _psi4_image_config(tmp_path))
    entry = _registry_entry()
    process = AsyncMock(pid=4242, returncode=0)
    process.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))
    with patch("simstack.core.run_docker.context", mock_context), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn, \
         patch("simstack.core.run_docker._harmonize_docker_task_dir", AsyncMock()):
        assert (await run_docker_with_outcome(entry)).success
    call = spawn.await_args_list[0]
    assert call.kwargs["env"]["SIMSTACK_EXECUTION_GENERATION"] == "original-generation"
    assert call.kwargs["env"]["SIMSTACK_PENDING_FAILURE_DIR"] == f"{CONTAINER_WORKDIR}/.pending-execution-failures"
    assert "SIMSTACK_EXECUTION_GENERATION" in call.args
    assert "SIMSTACK_PENDING_FAILURE_DIR" in call.args


@pytest.mark.asyncio
async def test_lost_process_report_does_not_fail_running_container(tmp_path):
    mock_context = _mock_context(tmp_path, _psi4_image_config(tmp_path))
    mock_context.server.tasks.record_process.side_effect = OSError("API unavailable")
    entry = _registry_entry()
    process = AsyncMock(pid=4242, returncode=0)
    process.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))
    with patch("simstack.core.run_docker.context", mock_context), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
         patch("simstack.core.run_docker._harmonize_docker_task_dir", AsyncMock()):
        assert (await run_docker_with_outcome(entry)).success
    process.communicate.assert_awaited_once()
    mock_context.server.tasks.fail_attempt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_case", ["running", "query_unavailable", "still_running", "cli_already_exited", "receipt_write_failure"])
async def test_local_docker_owner_confirms_daemon_stop_before_acknowledgement(tmp_path, monkeypatch, stop_case):
    from simstack.core.services import task_termination
    from simstack.core.run_docker import docker_execution_name

    active = _mock_context(tmp_path, _psi4_image_config(tmp_path))
    active.server.requests.authorization = "local"
    active.server.requests.token = "local-execution-test-token"
    entry = _registry_entry()
    from simstack.models.document import ObjectId
    entry.id = str(ObjectId())
    entry.parameters.in_docker = True
    entry.parameters.queue = "default"
    entry.job_id = None
    entry._execution_local = True
    entry._execution_resource = "local"
    entry._execution_start_id = "original-start"
    execution = active.server.tasks.execution(entry)
    entry._execution_owner = {"execution": execution, "process_id": os.getpid()}
    active.server.tasks.app_status = TaskStatus.TERMINATING
    active.server.tasks.container_state = {"Status": "running", "ExitCode": 0}
    entry.error = "Cancelled by final consumer"
    finished = asyncio.Event()
    events = []

    class DockerCLI:
        pid = os.getpid() + 100000
        returncode = 0 if stop_case == "cli_already_exited" else None

        async def communicate(self):
            await finished.wait()
            return b"", b""

        async def wait(self):
            await finished.wait()
            return self.returncode

    process = DockerCLI()
    if process.returncode is not None:
        finished.set()

    async def spawn(*command, **kwargs):
        assert command[:2] == ("docker", "run")
        docker_cidfile_path(tmp_path / entry.name / str(entry.id)).write_text("owned-container")
        return process

    inspections = 0

    def inspect(name):
        nonlocal inspections
        inspections += 1
        assert name == docker_execution_name(active.server, execution)
        events.append(("inspect", active.server.tasks.container_state["Status"]))
        if stop_case == "query_unavailable" and inspections == 1:
            assert not any(event[0] == "ack" for event in events)
            raise RuntimeError("Docker daemon temporarily unavailable")
        return {"Id": "owned-container", "State": dict(active.server.tasks.container_state)}

    kills = 0

    async def command(args):
        nonlocal kills
        assert args == ["docker", "kill", "owned-container"]
        kills += 1
        events.append(("docker-kill", kills))
        if stop_case != "still_running" or kills > 1:
            active.server.tasks.container_state = {"Status": "exited", "ExitCode": 137}
        return ""

    def kill_cli(pid, *args):
        assert pid == process.pid
        assert active.server.tasks.container_state["Status"] == "exited"
        events.append(("cli-stopped", pid))
        process.returncode = -9
        finished.set()

    async def acknowledge(observed, error):
        assert active.server.tasks.execution(observed) == execution
        assert error == entry.error
        assert active.server.tasks.container_state["Status"] == "exited"
        assert process.returncode is not None and finished.is_set()
        assert events[-1][0] in {"cli-stopped", "inspect"}
        events.append(("ack", error))
        active.server.tasks.app_status = TaskStatus.FAILED
        return SimpleNamespace(**{**vars(entry), "status": TaskStatus.FAILED})

    active.server.tasks.termination_confirmed = AsyncMock(side_effect=acknowledge)
    record_spawn = []

    def record_launched_process(server, task, pid):
        record_spawn.append(pid)
        if stop_case == "receipt_write_failure":
            raise OSError("Could not persist the launched process receipt")
    monkeypatch.setattr("simstack.core.run_docker.context", active)
    monkeypatch.setattr("simstack.core.run_docker.pull_docker_image", AsyncMock())
    monkeypatch.setattr("simstack.core.run_docker._harmonize_docker_task_dir", AsyncMock())
    monkeypatch.setattr("simstack.core.run_docker.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("simstack.core.run_docker.inspect_docker_execution", inspect)
    monkeypatch.setattr("simstack.runner_runtime.launch_claims.record_spawn", record_launched_process)
    monkeypatch.setattr(task_termination, "_run_command", command)
    monkeypatch.setattr(task_termination, "_kill_process_tree", kill_cli)

    result = await asyncio.wait_for(run_docker_with_outcome(entry), timeout=8)
    assert not result.success and result.error == entry.error
    assert record_spawn == [os.getpid()]
    assert entry._execution_owner == {"execution": execution, "process_id": os.getpid()}
    active.server.tasks.record_process.assert_awaited_once_with(
        entry, os.getpid(), host_owner=True, container_backend="docker")
    assert active.server.tasks.termination_confirmed.await_count >= 1
    assert kills == (2 if stop_case == "still_running" else 1)


@pytest.mark.asyncio
async def test_parent_handle_verifies_container_after_admitted_owner_exits(tmp_path, monkeypatch):
    from simstack.core.services import task_termination

    active = _mock_context(tmp_path, _psi4_image_config(tmp_path))
    from simstack.models.document import ObjectId
    entry = _registry_entry()
    entry.id = str(ObjectId())
    entry._execution_local = True
    entry.parameters.in_docker = True
    entry.job_id = None
    entry.process_id = None
    entry._execution_start_id = "original-start"
    entry._execution_owner = {"execution": active.server.tasks.execution(entry), "process_id": 4242}
    owner = SimpleNamespace(pid=4242, returncode=None)
    async def exited():
        owner.returncode = -9
        return -9
    owner.wait = AsyncMock(side_effect=exited)
    commands = AsyncMock()
    monkeypatch.setattr(task_termination, "_run_command", commands)
    observations = [{"Id": "owned-container", "State": {"Status": "running"}},
                    {"Id": "owned-container", "State": {"Status": "exited"}}]
    with patch("simstack.core.run_docker.inspect_docker_execution", side_effect=observations) as inspect:
        await task_termination.stop_task_execution(entry, server=active.server,
            runner_pid=os.getpid(), workdir=tmp_path, local_process=owner)
    owner.wait.assert_awaited_once()
    commands.assert_awaited_once_with(["docker", "kill", "owned-container"])
    assert inspect.call_count == 2
