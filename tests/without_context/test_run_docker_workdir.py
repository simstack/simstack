from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from simstack.core.run_docker import (
    container_resource_args,
    docker_cpu_limit,
    docker_memory_limit,
    ensure_host_task_workdir,
    run_docker,
)
from simstack.core.definitions import TaskStatus
from simstack.util.resource_config import ResourceConfig


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
    proc.communicate = AsyncMock(return_value=(b"", b""))

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
    proc.communicate = AsyncMock(return_value=(b"", b""))

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
