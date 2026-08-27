from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from simstack.core.run_docker import (
    CONTAINER_WORKDIR,
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
    assert container_resource_args("apptainer", slurm) == [
        "--cpus",
        "8",
        "--memory",
        "8G",
    ]
    assert container_resource_args("docker", None) == []


def _registry_entry(
    name: str = "psi4_calculator",
    resource: str = "local",
    slurm_parameters=None,
):
    return SimpleNamespace(
        id="abc123",
        name=name,
        parameters=SimpleNamespace(resource=resource, slurm_parameters=slurm_parameters),
        status=None,
        error=None,
    )


def _mock_context(tmp_path: Path, resource_config: ResourceConfig):
    (tmp_path / "simstack.toml").write_text("", encoding="utf-8")
    return SimpleNamespace(
        resource_config=resource_config,
        config=SimpleNamespace(
            resource="local",
            workdir=tmp_path,
            project_root=tmp_path,
            connection_string="mongodb://localhost:27017",
            db_name="test_db",
        ),
        db=SimpleNamespace(save=AsyncMock()),
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
    proc = AsyncMock()
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

    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
    ):
        result = await run_docker(registry_entry)

    assert result is True
    cmd = mock_exec.await_args.args
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

    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_SUCCESS_PROTOCOL, b""))

    with (
        patch("simstack.core.run_docker.context", mock_context),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
    ):
        result = await run_docker(registry_entry)

    assert result is True
    cmd = list(mock_exec.await_args.args)
    image_index = cmd.index("molecular-qm-psi4:latest")
    assert cmd[:image_index][cmd[:image_index].index("--cpus") + 1] == "8"
    assert cmd[:image_index][cmd[:image_index].index("--memory") + 1] == "8g"
    assert cmd.index("--cpus") < image_index
    assert cmd.index("--memory") < image_index


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
    mock_context.db.save.assert_awaited()
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
        proc = AsyncMock()
        proc.returncode = returncode
        stdout = _SUCCESS_PROTOCOL if returncode == 0 else b""
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        return proc

    return _exec


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
    cmd = list(mock_exec.await_args.args)
    assert "--cidfile" in cmd
    cidfile = Path(cmd[cmd.index("--cidfile") + 1])
    expected = tmp_path / "psi4_calculator" / "abc123" / ".docker_cid"
    assert cidfile == expected
    assert f"{tmp_path}:{CONTAINER_WORKDIR}" in cmd
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
async def test_run_docker_generic_failure_is_not_oom(tmp_path: Path):
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
    ):
        result = await run_docker(registry_entry)

    assert result is False
    assert registry_entry.status == TaskStatus.FAILED
    assert registry_entry.error is not None
    assert "exit 1" in registry_entry.error
    assert "out of memory" not in registry_entry.error.lower()
    assert "Likely OOM" not in registry_entry.error
    assert "SIGKILL" not in registry_entry.error


def test_inspect_docker_oomkilled_parses_true_false():
    with patch("simstack.core.run_docker.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="true\n")
        assert inspect_docker_oomkilled("cid") is True
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="false\n")
        assert inspect_docker_oomkilled("cid") is False
        mock_run.return_value = SimpleNamespace(returncode=1, stdout="")
        assert inspect_docker_oomkilled("cid") is None


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

    proc = AsyncMock()
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
    assert not any("site-packages/simstack" in argument for argument in command)


@pytest.mark.asyncio
async def test_run_docker_redacts_and_bounds_database_uri_in_child_errors(
    tmp_path: Path, caplog
):
    secret = "mongodb://docker-user:docker-password@db.internal:27017/simstack"
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    mock_context.config.connection_string = secret
    registry_entry = _registry_entry()

    proc = AsyncMock()
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
    proc = AsyncMock()
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
    update_one = AsyncMock()
    mock_context.db.load_task_by_id = AsyncMock(return_value=child_entry)
    mock_context.db.get_collection = lambda model: SimpleNamespace(
        update_one=update_one
    )
    proc = AsyncMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(
        return_value=(
            encode_run_node_result(
                RunNodeResult(False, "exception", "child error")
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
    assert child_entry.info_files == ["child-log"]
    assert child_entry.return_kind == "exception"
    assert child_entry.message == "child message"
    assert mock_context.db.save.await_count == 1
    update = update_one.await_args.args[1]["$set"]
    assert set(update) == {"status", "error"}


@pytest.mark.asyncio
async def test_run_docker_redacts_database_uri_from_spawn_exception(
    tmp_path: Path, caplog
):
    secret = "mongodb://docker-user:docker-password@db.internal:27017/simstack"
    resource_config = _psi4_image_config(tmp_path)
    mock_context = _mock_context(tmp_path, resource_config)
    mock_context.config.connection_string = secret
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
