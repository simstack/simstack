from types import SimpleNamespace

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Parameters, SlurmParameters


class FakeDatabase:
    def __init__(self):
        self.saved = []

    async def save(self, item):
        self.saved.append(item)
        return item


def _slurm_registry(name: str, parameters: Parameters, parent_ids=None) -> NodeRegistry:
    return NodeRegistry(
        name=name,
        status=TaskStatus.SUBMITTED,
        function_hash=f"{name}-function-hash",
        arg_hash=f"{name}-arg-hash",
        func_mapping=f"tests:{name}",
        parameters=parameters,
        parent_ids=parent_ids or [],
    )


@pytest.mark.asyncio
async def test_submit_node_does_not_persist_generated_startup_commands(
    tmp_path, monkeypatch, initialized_context
):
    project_root = tmp_path / "project"
    python_path = tmp_path / "simstack-model"
    workdir = tmp_path / "work"
    project_root.mkdir()
    python_path.mkdir()

    monkeypatch.setattr(
        context,
        "config",
        SimpleNamespace(
            project_root=project_root,
            python_paths=[python_path],
            workdir=workdir,
            environment_start="",
            docker=False,
            resource="local",
        ),
    )
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Submitted batch job 12345\n",
            stderr="",
        ),
    )

    parent_parameters = Parameters(
        resource="local",
        queue="slurm-queue",
        slurm_parameters=SlurmParameters(
            nodes=2,
            tasks=None,
            tasks_per_node=1,
            mem="8G",
            time="02:00:00",
            job_name="template-job",
            output="/template/%j.out",
            error="/template/%j.err",
            startup_commands=["module load orca"],
            chdir="/template/work",
        ),
    )
    parent = _slurm_registry("parent", parent_parameters)

    await submit_node(parent)

    parent_script = (
        workdir / parent.name / str(parent.id) / "slurm_script.sh"
    ).read_text()
    assert f"run_node --node-id {parent.id}" in parent_script
    assert parent_script.count("run_node --node-id") == 1
    assert "#SBATCH --job-name=parent." in parent_script
    assert "module load orca" in parent_script
    assert parent.status == TaskStatus.SLURM_QUEUED
    assert parent.job_id == "12345"

    parent_slurm = parent.parameters.slurm_parameters
    assert parent_slurm.job_name == "template-job"
    assert parent_slurm.output == "/template/%j.out"
    assert parent_slurm.error == "/template/%j.err"
    assert parent_slurm.chdir == "/template/work"
    assert parent_slurm.startup_commands == ["module load orca"]

    child = _slurm_registry("child", parent.parameters, parent_ids=[parent.id])

    await submit_node(child)

    child_script = (
        workdir / child.name / str(child.id) / "slurm_script.sh"
    ).read_text()
    assert f"run_node --node-id {child.id}" in child_script
    assert f"run_node --node-id {parent.id}" not in child_script
    assert child_script.count("run_node --node-id") == 1
    assert "module load orca" in child_script
    assert child.parameters.slurm_parameters.startup_commands == ["module load orca"]
