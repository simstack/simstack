from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.context import context
from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Parameters, SlurmParameters
from simstack.runner_runtime.execution_adapter import RunnerExecutionAdapterConfig


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
async def test_submit_node_uses_claim_bound_api_without_mutating_parameters(
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
            environment_start="module load trusted-environment",
            docker=False,
            resource="local",
        ),
    )
    monkeypatch.setattr(context, "_runner_mode", True)
    monkeypatch.setattr(
        context,
        "_runner_adapter",
        RunnerExecutionAdapterConfig.from_mapping(
            {
                "resource": {
                    "program": {
                        "parent": {"worker_environment_allowlist": []},
                        "child": {"worker_environment_allowlist": []},
                    },
                    "setup": {
                        "slurm_worker_command": ["configured-run-node"],
                        "worker_network_guard_command": ["network-guard", "--"],
                        "slurm_submit_command": ["configured-sbatch"],
                    },
                }
            },
            resource="resource",
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
            startup_commands=[],
            chdir="/template/work",
        ),
    )
    parent = _slurm_registry("parent", parent_parameters)
    runner_api = SimpleNamespace(
        server_url="https://server.example",
        runner_token="execution-token",
        update_task_status=AsyncMock(return_value={}),
    )

    await submit_node(
        parent,
        claim_id="parent-claim",
        attempt=1,
        runner_api=runner_api,
    )

    parent_script = (workdir / "tasks" / str(parent.id) / "slurm_script.sh").read_text()
    assert f"configured-run-node --node-id {parent.id}" in parent_script
    assert parent_script.count("configured-run-node --node-id") == 1
    assert "network-guard -- configured-run-node" in parent_script
    assert f"#SBATCH --job-name=simstack-{parent.id}" in parent_script
    assert "module load trusted-environment" in parent_script
    assert "SIMSTACK_TASK_CLAIM_ID" in parent_script
    assert parent.status == TaskStatus.SUBMITTED
    assert parent.job_id is None

    parent_slurm = parent.parameters.slurm_parameters
    assert parent_slurm.job_name == "template-job"
    assert parent_slurm.output == "/template/%j.out"
    assert parent_slurm.error == "/template/%j.err"
    assert parent_slurm.chdir == "/template/work"
    assert parent_slurm.startup_commands == []

    child = _slurm_registry("child", parent.parameters, parent_ids=[parent.id])

    await submit_node(
        child,
        claim_id="child-claim",
        attempt=2,
        runner_api=runner_api,
    )

    child_script = (workdir / "tasks" / str(child.id) / "slurm_script.sh").read_text()
    assert f"configured-run-node --node-id {child.id}" in child_script
    assert f"configured-run-node --node-id {parent.id}" not in child_script
    assert child_script.count("configured-run-node --node-id") == 1
    assert "module load trusted-environment" in child_script
    assert child.parameters.slurm_parameters.startup_commands == []
    assert runner_api.update_task_status.await_count == 4
    first_status = runner_api.update_task_status.await_args_list[0]
    assert first_status.kwargs["claim_id"] == "parent-claim"
    assert first_status.kwargs["attempt"] == 1
    assert first_status.kwargs["status"] == TaskStatus.SLURM_QUEUED.value
    published_job = runner_api.update_task_status.await_args_list[1]
    assert published_job.kwargs["expected_status"] == TaskStatus.SLURM_QUEUED.value
    assert published_job.kwargs["job_id"] == "12345"
