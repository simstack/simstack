from __future__ import annotations

from pathlib import Path

import pytest

from simstack.runner_runtime.execution_adapter import (
    ContainerBackend,
    RunnerExecutionAdapterConfig,
)


def _configuration(tmp_path: Path) -> dict[str, object]:
    return {
        "cluster": {
            "setup": {
                "slurm_worker_command": ["run-node"],
                "worker_network_guard_command": ["guard", "--"],
                "slurm_submit_command": ["sbatch-adapter"],
                "slurm_status_command": ["squeue-adapter", "{job_id}"],
                "tmp_base_dir": str(tmp_path / "scratch"),
            },
            "program": {
                "calculation": {
                    "local_worker_command": ["local-launcher", "run-node"],
                    "worker_network_guard_command": ["local-guard", "--"],
                    "worker_environment_allowlist": ["PROGRAM_LICENSE"],
                    "container_backend": "docker",
                    "container_command": ["container-adapter"],
                    "container_worker_command": ["run-node"],
                    "container_image": "worker:stable",
                    "container_project_root": "/workspace/source",
                    "container_workdir": "/workspace/tasks",
                    "use_tmp": True,
                }
            },
        }
    }


def test_execution_adapter_projects_typed_local_container_and_slurm(tmp_path):
    config = RunnerExecutionAdapterConfig.from_mapping(
        _configuration(tmp_path),
        resource="cluster",
        config_directory=tmp_path,
    )

    local = config.local("calculation")
    assert local.guarded_worker_command == (
        "local-guard",
        "--",
        "local-launcher",
        "run-node",
    )
    assert local.boundary.environment_allowlist == ("PROGRAM_LICENSE",)

    container = config.container("calculation")
    assert container.backend is ContainerBackend.DOCKER
    assert container.launcher_command == ("container-adapter",)
    assert container.guarded_worker_command == ("local-guard", "--", "run-node")
    assert str(container.project_root) == "/workspace/source"
    assert str(container.workdir) == "/workspace/tasks"

    slurm = config.slurm("calculation")
    assert slurm.submit_command == ("sbatch-adapter",)
    assert slurm.guarded_worker_command == ("guard", "--", "run-node")
    assert slurm.use_tmp is True
    assert slurm.task_tmp_dir("task-1") == tmp_path / "scratch" / "task-1"
    assert config.slurm_status().command_for("421") == (
        "squeue-adapter",
        "421",
    )
    assert config.worker_environment("calculation") == ("PROGRAM_LICENSE",)


def test_execution_adapter_requires_real_file(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        RunnerExecutionAdapterConfig.from_file(
            tmp_path / "missing.toml",
            "cluster",
        )


def test_execution_adapter_requires_selected_resource(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[other]\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="resource 'cluster' is not configured"):
        RunnerExecutionAdapterConfig.from_file(config_path, "cluster")


def test_execution_adapter_fails_closed_without_worker_contract(tmp_path):
    config = RunnerExecutionAdapterConfig.from_mapping(
        {"cluster": {"program": {"calculation": {}}}},
        resource="cluster",
        config_directory=tmp_path,
    )

    with pytest.raises(RuntimeError, match="local_worker_command"):
        config.local("calculation")
    with pytest.raises(RuntimeError, match="container_backend"):
        config.container("calculation")
    with pytest.raises(RuntimeError, match="slurm_submit_command"):
        config.slurm("calculation")
    with pytest.raises(RuntimeError, match="slurm_status_command"):
        config.slurm_status()
    with pytest.raises(RuntimeError, match="explicit list"):
        config.worker_environment("calculation")


def test_execution_adapter_rejects_tmp_traversal(tmp_path):
    config = RunnerExecutionAdapterConfig.from_mapping(
        _configuration(tmp_path),
        resource="cluster",
        config_directory=tmp_path,
    )

    with pytest.raises(ValueError, match="single path component"):
        config.slurm("calculation").task_tmp_dir("../escape")


def test_execution_adapter_requires_tmp_root_when_recovery_is_enabled(tmp_path):
    configuration = _configuration(tmp_path)
    del configuration["cluster"]["setup"]["tmp_base_dir"]
    config = RunnerExecutionAdapterConfig.from_mapping(
        configuration,
        resource="cluster",
        config_directory=tmp_path,
    )

    with pytest.raises(RuntimeError, match="explicit non-empty tmp_base_dir"):
        config.slurm("calculation")
