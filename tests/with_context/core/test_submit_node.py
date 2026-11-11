from types import SimpleNamespace
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from simstack.core.context import context
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Parameters, SlurmParameters
from simstack.runner_runtime.launch_claims import acquire_work


def completed_submission(*, returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, pid=os.getpid(),
        communicate=lambda **kwargs: (stdout, stderr))


async def claimed_task(server_storage, name, parameters):
    task = NodeRegistry(
        name=name,
        status="submitted",
        function_hash="function",
        arg_hash="input",
        func_mapping=f"tests:{name}",
        parameters=parameters,
    )
    await server_storage.save(task)
    acquired = await acquire_work(context.server, capacity=100)
    entry = next(item for item in acquired if item.id == task.id)
    object.__setattr__(entry, "_execution_start_id", str(uuid4()))
    return entry


def configure(tmp_path, monkeypatch, program=None):
    config = SimpleNamespace(
        project_root=tmp_path,
        python_paths=[tmp_path / "models"],
        workdir=tmp_path / "work",
        environment_start="",
        resource="test",
    )
    monkeypatch.setattr(context, "config", config)
    calls = []
    monkeypatch.setattr("simstack.runner_runtime.slurm_submission_intents.process_identity", lambda pid: "fixture-process")

    def get_program(name, resource=None):
        calls.append((name, resource))
        return program or {}

    monkeypatch.setattr(
        context, "_resource_config", SimpleNamespace(get_program=get_program)
    )
    monkeypatch.setattr("simstack.core.submit_node.process_is_in_docker", lambda: False)
    return calls


@pytest.mark.asyncio
async def test_generated_script_preserves_allocation_and_original_parameters(
    tmp_path, monkeypatch, server_storage
):
    configure(tmp_path, monkeypatch)
    secret = "mongodb://slurm-env:slurm-password@db.internal:27017/simstack"
    monkeypatch.setenv("SIMSTACK_DB_CONNECTION_STRING", secret)
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://server.example/api")
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "runner-test-token")
    submitted = []

    def sbatch(*args, **kwargs):
        submitted.append((args, kwargs))
        return completed_submission(returncode=0, stdout="12345;cluster\n", stderr="")

    monkeypatch.setattr("simstack.core.submit_node.subprocess.Popen", sbatch)
    params = SlurmParameters(
        nodes=2,
        tasks=None,
        tasks_per_node=1,
        mem="8G",
        time="02:00:00",
        job_name="template-job",
        output="/template/%j.out",
        error="/template/%j.err",
        chdir="/template/work",
        startup_commands=["module load orca"],
    )
    entry = await claimed_task(
        server_storage,
        "parent",
        Parameters(resource="test", queue="slurm-queue", slurm_parameters=params),
    )
    original = entry.parameters.slurm_parameters.model_dump()
    assert await submit_node(entry)
    script = (
        context.config.workdir / entry.name / str(entry.id) / "slurm_script.sh"
    ).read_text()
    assert script.count(f"run_node --node-id {entry.id}") == 1
    assert "#SBATCH --job-name=ss-" in script
    assert "#SBATCH --nodes=2" in script
    assert "#SBATCH --mem=8G" in script
    assert "module load orca" in script
    assert secret not in script
    assert "SIMSTACK_DB_CONNECTION_STRING" not in submitted[0][1]["env"]
    assert submitted[0][1]["env"]["SIMSTACK_RUNNER_TOKEN"] == "runner-test-token"
    assert entry.parameters.slurm_parameters.model_dump() == original
    stored = await context.server.tasks.get_task(entry.id)
    assert stored.status == "slurm_queued"
    assert stored.job_id == "12345"


@pytest.mark.asyncio
@pytest.mark.parametrize("export", [None, "ALL", "NONE", "NIL", "CUSTOM_SETTING"])
async def test_restricted_slurm_export_preserves_launcher_identity(
    tmp_path, monkeypatch, server_storage, export
):
    configure(tmp_path, monkeypatch)
    real_popen = subprocess.Popen
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://server.example/api")
    token = "private'quoted\nrunner-token"
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", token)
    monkeypatch.setenv("SIMSTACK_DB_CONNECTION_STRING", "must-not-propagate")
    monkeypatch.setattr("simstack.core.submit_node.subprocess.Popen",
        lambda *args, **kwargs: completed_submission(stdout="12345\n"))
    target = tmp_path / "observed.json"
    monkeypatch.setattr("simstack.runner_runtime.execution_environment.task_launch_command",
        lambda *args: [sys.executable, "-c",
            "import json, os; from pathlib import Path; Path(" + repr(str(target)) +
            ").write_text(json.dumps(dict(os.environ)))"])
    entry = await claimed_task(server_storage, "export_test", Parameters(resource="test",
        queue="slurm-queue", slurm_parameters=SlurmParameters(export=export)))
    from bson import ObjectId
    generation = str(uuid4())
    await server_storage.raw_database["node_registry"].update_one(
        {"_id": ObjectId(entry.id)}, {"$set": {"_runner_generation": generation}})
    object.__setattr__(entry, "_execution_generation", generation)
    assert await submit_node(entry)
    workdir = context.config.workdir / entry.name / str(entry.id)
    script = workdir / "slurm_script.sh"
    assert token not in script.read_text()
    if export is not None:
        assert f"#SBATCH --export={export}" in script.read_text()
    launch_environment, = workdir.glob("slurm_environment_*.sh")
    assert launch_environment.stat().st_mode & 0o777 == 0o600
    assert "SIMSTACK_DB_CONNECTION_STRING" not in launch_environment.read_text()
    # Execute the generated wrapper in the stripped environment Slurm permits
    # under NONE/NIL. No Slurm installation or scientific program is mocked here.
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    (tmp_path / ".bashrc").write_text("")
    subprocess.run(["/bin/bash", str(script)], env={"HOME": str(tmp_path), "PATH": os.defpath}, check=True)
    observed = json.loads(target.read_text())
    assert observed["SIMSTACK_RUNNER_TOKEN"] == token
    assert observed["SIMSTACK_EXECUTION_TASK"] == str(entry.id)
    assert observed["SIMSTACK_EXECUTION_CLAIM"] == entry._execution_claim_id
    assert observed["SIMSTACK_EXECUTION_START"] == entry._execution_start_id
    assert observed["SIMSTACK_EXECUTION_GENERATION"] == entry._execution_generation
    assert "SIMSTACK_DB_CONNECTION_STRING" not in observed


@pytest.mark.asyncio
async def test_slurm_docker_uses_task_resource_program(
    tmp_path, monkeypatch, server_storage
):
    calls = configure(
        tmp_path, monkeypatch, {"docker_image": "selected-resource-image:latest"}
    )
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.Popen",
        lambda *args, **kwargs: completed_submission(
            returncode=0, stdout="456\n", stderr=""
        ),
    )
    entry = await claimed_task(
        server_storage,
        "docker_child",
        Parameters(
            resource="test",
            queue="slurm-queue",
            in_docker=True,
            slurm_parameters=SlurmParameters(),
        ),
    )
    assert await submit_node(entry)
    assert calls == [("docker_child", "test")]
    assert entry.parameters.in_docker
    script = (
        context.config.workdir / entry.name / str(entry.id) / "slurm_script.sh"
    ).read_text()
    assert "--resource test" in script
    assert "udocker run" not in script


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_signal", [None, "B:SIGUSR1@120"])
async def test_scratch_checkpoint_installs_handler_and_preserves_configured_signal(
    tmp_path, monkeypatch, server_storage, configured_signal
):
    configure(tmp_path, monkeypatch, {"use_tmp": True})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(context._resource_config, "tmp_dir", lambda task_id: scratch, raising=False)
    monkeypatch.setattr("simstack.core.submit_node.subprocess.Popen",
        lambda *args, **kwargs: completed_submission(stdout="42\n"))
    entry = await claimed_task(server_storage, "checkpoint_child", Parameters(resource="test", queue="slurm-queue",
        slurm_parameters=SlurmParameters(signal=configured_signal)))
    assert await submit_node(entry)
    script = (context.config.workdir / entry.name / str(entry.id) / "slurm_script.sh").read_text()
    assert "#SBATCH --signal=" + (configured_signal or "B:SIGUSR1@60") in script
    assert "slurm_checkpoint.sh" in script
    assert str(scratch) in script
    assert entry.parameters.slurm_parameters.signal == configured_signal


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Slurm batch execution requires a POSIX host")
async def test_actual_generated_batch_propagates_the_child_process_exit(
    tmp_path, monkeypatch, server_storage
):
    configure(tmp_path, monkeypatch)
    marker = tmp_path / "child-task-id"
    code = "import sys; from pathlib import Path; Path(" + repr(str(marker)) + ").write_text(sys.argv[1]); raise SystemExit(17)"
    monkeypatch.setattr("simstack.runner_runtime.execution_environment.task_launch_command",
        lambda task, *args: [sys.executable, "-c", code, str(task.id)])
    native_popen = subprocess.Popen
    monkeypatch.setattr("simstack.core.submit_node.subprocess.Popen",
        lambda *args, **kwargs: completed_submission(stdout="42\n"))
    entry = await claimed_task(server_storage, "failed_python_child", Parameters(resource="test", queue="slurm-queue",
        slurm_parameters=SlurmParameters()))
    assert await submit_node(entry)
    (tmp_path / ".bashrc").write_text("")
    environment = {**os.environ, "HOME": str(tmp_path)}
    script = context.config.workdir / entry.name / str(entry.id) / "slurm_script.sh"
    with native_popen(["bash", str(script)], env=environment) as batch:
        assert batch.wait(timeout=5) == 17
    assert marker.read_text() == str(entry.id)


@pytest.mark.asyncio
async def test_sbatch_failure_is_bounded_sanitized_and_stored(
    tmp_path, monkeypatch, server_storage, caplog
):
    configure(tmp_path, monkeypatch)
    secret = "mongodb://slurm-user:slurm-password@db.internal:27017/simstack"
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.Popen",
        lambda *args, **kwargs: completed_submission(
            returncode=1, stdout="", stderr="y" * 5000 + secret
        ),
    )
    entry = await claimed_task(
        server_storage,
        "failed_child",
        Parameters(
            resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters()
        ),
    )
    assert not await submit_node(entry)
    stored = await context.server.tasks.get_task(entry.id)
    assert stored.status == "failed"
    assert len(stored.error) <= 4096
    assert secret not in stored.error
    assert "slurm-password" not in caplog.text


@pytest.mark.asyncio
async def test_fast_completion_survives_delayed_submission_acceptance(
    tmp_path, monkeypatch, server_storage
):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.Popen",
        lambda *args, **kwargs: completed_submission(
            returncode=0, stdout="789\n", stderr=""
        ),
    )
    entry = await claimed_task(
        server_storage,
        "fast_child",
        Parameters(
            resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters()
        ),
    )
    accept = context.server.slurm.scheduler_accepted

    async def complete_then_accept(execution, submission_id, job_id):
        admitted = await context.server.slurm.start_incarnation(
            execution, submission_id, job_id, 0, os.getpid())
        started = await context.server.tasks.start_attempt(admitted["task"], process_id=os.getpid())
        await context.server.tasks.complete_attempt(started, return_kind="none")
        return await accept(execution, submission_id, job_id)

    monkeypatch.setattr(
        context.server.slurm, "scheduler_accepted", complete_then_accept
    )
    assert await submit_node(entry)
    stored = await context.server.tasks.get_task(entry.id)
    assert stored.status == "completed"
    assert stored.job_id == "789"


@pytest.mark.asyncio
async def test_submission_from_container_is_rejected_before_sbatch(
    tmp_path, monkeypatch, server_storage
):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr("simstack.core.submit_node.process_is_in_docker", lambda: True)
    calls = []
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.Popen",
        lambda *args, **kwargs: calls.append(args),
    )
    entry = await claimed_task(
        server_storage,
        "container_sbatch_child",
        Parameters(
            resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters()
        ),
    )
    with pytest.raises(RuntimeError, match="host Runner"):
        await submit_node(entry)
    stored = await context.server.tasks.get_task(entry.id)
    assert stored.status == "failed"
    assert "host Runner" in stored.error
    assert not calls
    assert not list(context.config.workdir.rglob("slurm_script.sh"))


@pytest.mark.asyncio
async def test_missing_sbatch_is_a_known_submission_failure(
    tmp_path, monkeypatch, server_storage
):
    configure(tmp_path, monkeypatch)

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("sbatch executable not found")

    monkeypatch.setattr("simstack.core.submit_node.subprocess.Popen", unavailable)
    entry = await claimed_task(
        server_storage,
        "missing_scheduler",
        Parameters(
            resource="test", queue="slurm-queue", slurm_parameters=SlurmParameters()
        ),
    )
    assert not await submit_node(entry)
    stored = await context.server.tasks.get_task(entry.id)
    assert stored.status == "failed"
    assert "sbatch executable not found" in stored.error
