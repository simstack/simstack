from types import SimpleNamespace
from pathlib import Path
import pytest

from simstack.runner_runtime.container_limits import container_resource_args, docker_cpu_limit, docker_memory_limit
from simstack.runner_runtime.config import runner_task_workdir


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




def test_task_workdir_is_identity_only_and_rejects_traversal(tmp_path: Path):
    assert runner_task_workdir(tmp_path, "task-one") == tmp_path / "tasks" / "task-one"
    with pytest.raises(ValueError, match="single path component"):
        runner_task_workdir(tmp_path, "../escape")
