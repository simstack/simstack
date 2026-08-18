import json
import shlex
import sys
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("queue_save_behavior", "expected_success", "expected_status"),
    [
        ("normal", True, TaskStatus.SLURM_QUEUED),
        ("compute-advanced", True, TaskStatus.COMPLETED),
        ("persistence-error-after-compute-advanced", False, TaskStatus.COMPLETED),
    ],
)
async def test_repository_submit_uses_pinned_context_without_shell_interpolation(
    tmp_path,
    monkeypatch,
    initialized_context,
    queue_save_behavior: str,
    expected_success: bool,
    expected_status: TaskStatus,
):
    project_root = tmp_path / "trusted project '$(touch launcher-owned)'"
    repository_checkout = tmp_path / "uploaded checkout '$(touch repo-owned)'"
    workdir = tmp_path / "shared workdir"
    project_root.mkdir()
    repository_checkout.mkdir()
    resource = "cluster; touch resource-owned"

    monkeypatch.setattr(
        context,
        "config",
        SimpleNamespace(
            project_root=project_root,
            python_paths=[tmp_path / "legacy-models"],
            workdir=workdir,
            environment_start="",
            docker=False,
            resource=resource,
            server_url="https://runner.example.invalid",
            server_token="runner-token",
        ),
    )
    monkeypatch.delenv("SIMSTACK_SERVER_URL", raising=False)
    monkeypatch.delenv("SIMSTACK_RUNNER_TOKEN", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(repository_checkout))
    submissions = []

    def capture_submission(*args, **kwargs):
        submissions.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Submitted batch job 12345\n",
            stderr="",
        )

    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.run",
        capture_submission,
    )
    registry_entry = _slurm_registry(
        "pinned_task",
        Parameters(
            resource="test",
            queue="slurm-queue",
            slurm_parameters=SlurmParameters(nodes=1),
        ),
    )
    registry_entry.status = TaskStatus.RETRIEVED
    registry_entry = await context.db.save(registry_entry)
    if queue_save_behavior != "normal":
        collection = context.db.get_collection(NodeRegistry)
        original_get_collection = context.db.get_collection

        class RacingCollection:
            update_count = 0

            def __getattr__(self, name):
                return getattr(collection, name)

            async def update_one(self, query, update):
                self.update_count += 1
                if self.update_count == 1:
                    await collection.update_one(
                        {"_id": registry_entry.id},
                        {"$set": {"status": TaskStatus.COMPLETED.value}},
                    )
                    if queue_save_behavior == "persistence-error-after-compute-advanced":
                        raise RuntimeError("queue state persistence failed")
                return await collection.update_one(query, update)

        racing_collection = RacingCollection()
        monkeypatch.setattr(
            context.db,
            "get_collection",
            lambda model: racing_collection
            if model is NodeRegistry
            else original_get_collection(model),
        )

    assert (
        await submit_node(
            registry_entry,
            repository_checkout=repository_checkout,
        )
        is expected_success
    )

    script_path = workdir / registry_entry.name / str(registry_entry.id) / "slurm_script.sh"
    script = script_path.read_text()
    expected_command = (
        f"{shlex.quote(sys.executable)} -m simstack.core.run_node "
        f"--node-id {shlex.quote(str(registry_entry.id))} "
        f"--resource {shlex.quote(resource)} "
        f"--project-root {shlex.quote(str(repository_checkout))} &"
    )
    assert expected_command in script
    assert "uv run" not in script
    assert "SIMSTACK_TASK_DB_CONNECTION_STRING" not in script
    assert f"export PYTHONPATH={repository_checkout}" not in script

    assert len(submissions) == 1
    arguments, options = submissions[0]
    (command,) = arguments
    assert command[0] == "/usr/bin/sbatch"
    assert command[-1] == str(script_path)
    assert command[1].startswith("--export=ALL,SIMSTACK_TASK_DB_NAME,")
    assert "SIMSTACK_SERVER_URL" in command[1].split(",")
    assert "SIMSTACK_RUNNER_TOKEN" in command[1].split(",")
    assert "shell" not in options
    assert "PYTHONPATH" not in options["env"]
    assert options["env"]["SIMSTACK_SERVER_URL"] == "https://runner.example.invalid"
    assert options["env"]["SIMSTACK_RUNNER_TOKEN"] == "runner-token"
    assert json.loads(options["env"]["SIMSTACK_TASK_PYTHON_PATHS"]) == [
        str(repository_checkout)
    ]
    persisted = await context.db.find_one(
        NodeRegistry,
        NodeRegistry.id == registry_entry.id,
    )
    assert persisted is not None
    assert persisted.status == expected_status
    await context.db.delete(persisted)
