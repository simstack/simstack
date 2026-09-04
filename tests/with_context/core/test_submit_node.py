from types import SimpleNamespace

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.submit_node import submit_node
from simstack.models import FloatData, NamedDataReference, NodeRegistry
from simstack.models.parameters import Parameters, SlurmParameters


class FakeDatabase:
    def __init__(self):
        self.saved = []

    async def save(self, item):
        self.saved.append(item)
        return item

    def get_collection(self, model):
        return self

    def _matches(self, item, query):
        for key, expected in query.items():
            actual = item.id if key == "_id" else getattr(item, key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    def _apply_update(self, item, update):
        for key, value in update.get("$set", {}).items():
            setattr(item, key, value)

    async def find_one_and_update(self, query, update):
        if not self.saved or not self._matches(self.saved[-1], query):
            return None
        self._apply_update(self.saved[-1], update)
        return {}

    async def update_one(self, query, update):
        if self.saved and self._matches(self.saved[-1], query):
            self._apply_update(self.saved[-1], update)
        return SimpleNamespace()


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

    secret = "mongodb://slurm-env:slurm-password@db.internal:27017/simstack"
    submitted = []

    def fake_sbatch(*args, **kwargs):
        submitted.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Submitted batch job 12345\n",
            stderr="",
        )

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
            connection_string=secret,
            db_name="slurm-database",
        ),
    )
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.run",
        fake_sbatch,
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
    assert secret not in parent_script
    assert submitted[0][1]["env"]["SIMSTACK_DB_CONNECTION_STRING"] == secret
    assert submitted[0][1]["env"]["SIMSTACK_DB_DATABASE"] == "slurm-database"
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
async def test_slurm_docker_uses_task_resource_program_and_keeps_task_flag(
    tmp_path, monkeypatch
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    workdir = tmp_path / "work"
    database = FakeDatabase()

    class RecordingResourceConfig:
        def __init__(self):
            self.calls = []

        def get_program(self, name, resource=None):
            self.calls.append((name, resource))
            return {"docker_image": "selected-resource-image:latest"}

    resource_config = RecordingResourceConfig()
    mock_context = SimpleNamespace(
        config=SimpleNamespace(
            project_root=project_root,
            python_paths=[],
            workdir=workdir,
            environment_start="",
            docker=False,
            resource="local",
            connection_string=None,
        ),
        resource_config=resource_config,
        db=database,
    )
    monkeypatch.setattr("simstack.core.submit_node.context", mock_context)
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Submitted batch job 456\n",
            stderr="",
        ),
    )
    registry = _slurm_registry(
        "docker_child",
        Parameters(
            resource="test",
            queue="slurm-queue",
            in_docker=True,
            slurm_parameters=SlurmParameters(nodes=1),
        ),
    )

    assert await submit_node(registry) is True

    assert resource_config.calls == [("docker_child", "test")]
    assert registry.parameters.in_docker is True
    script = (
        workdir / registry.name / str(registry.id) / "slurm_script.sh"
    ).read_text()
    assert "--resource test" in script
    assert "run_node --node-id" in script
    assert "udocker run" not in script


@pytest.mark.asyncio
async def test_sbatch_failure_output_is_bounded_and_sanitized(
    tmp_path, monkeypatch, caplog
):
    secret = "mongodb://slurm-user:slurm-password@db.internal:27017/simstack"
    project_root = tmp_path / "project"
    project_root.mkdir()
    workdir = tmp_path / "work"
    database = FakeDatabase()
    mock_context = SimpleNamespace(
        config=SimpleNamespace(
            project_root=project_root,
            python_paths=[],
            workdir=workdir,
            environment_start="",
            docker=False,
            resource="local",
            connection_string=secret,
        ),
        resource_config=None,
        db=database,
    )
    monkeypatch.setattr("simstack.core.submit_node.context", mock_context)
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="x" * 5000 + secret,
            stderr="y" * 5000 + secret,
        ),
    )
    registry = _slurm_registry(
        "failed_child",
        Parameters(
            resource="local",
            queue="slurm-queue",
            in_docker=False,
            slurm_parameters=SlurmParameters(nodes=1),
        ),
    )

    assert await submit_node(registry) is False

    assert registry.status == TaskStatus.FAILED
    assert len(registry.error or "") <= 4096
    assert secret not in (registry.error or "")
    assert "slurm-user" not in caplog.text
    assert "slurm-password" not in caplog.text


@pytest.mark.asyncio
async def test_fast_slurm_completion_is_not_rolled_back_after_sbatch_returns(
    tmp_path, monkeypatch
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    workdir = tmp_path / "work"
    database = FakeDatabase()
    mock_context = SimpleNamespace(
        config=SimpleNamespace(
            project_root=project_root,
            python_paths=[],
            workdir=workdir,
            environment_start="",
            docker=False,
            resource="local",
            connection_string=None,
            db_name="race-database",
        ),
        resource_config=None,
        db=database,
    )
    monkeypatch.setattr("simstack.core.submit_node.context", mock_context)
    registry = _slurm_registry(
        "fast_child",
        Parameters(
            resource="local",
            queue="slurm-queue",
            slurm_parameters=SlurmParameters(nodes=1),
        ),
    )
    completed_references = [
        NamedDataReference(
            variable_name="result",
            variable_mapping="simstack.models.FloatData",
            reference=FloatData(value=1.0).id,
        )
    ]

    def complete_before_sbatch_returns(*args, **kwargs):
        assert registry.status == TaskStatus.SLURM_QUEUED
        registry.status = TaskStatus.COMPLETED
        registry.results_references = completed_references
        return SimpleNamespace(
            returncode=0,
            stdout="Submitted batch job 789\n",
            stderr="",
        )

    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.run",
        complete_before_sbatch_returns,
    )

    assert await submit_node(registry) is True
    assert registry.status == TaskStatus.COMPLETED
    assert registry.results_references == completed_references
    assert registry.job_id == "789"
