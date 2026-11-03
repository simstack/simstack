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
async def test_submit_node_tmp_base_dir_shell_snippet_is_evaluated_at_runtime(
    tmp_path, monkeypatch, initialized_context
):
    """A shell-snippet tmp_base_dir (e.g. node-local $TMPDIR) must not be
    resolved on the submit host. Instead the snippet is embedded in the
    SIGUSR1 trap and evaluated at trap time on the compute node."""
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
            resource="justus-jinja",
        ),
    )
    monkeypatch.setattr(
        "simstack.core.submit_node.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Submitted batch job 12346\n",
            stderr="",
        ),
    )

    class StubResourceConfig:
        def get_program(self, name):
            return {"use_tmp": True}

        def get_setup_params(self):
            return {
                "tmp_base_dir": (
                    'if test -n "${TMPDIR}" -a -d "${TMPDIR}"; then\n'
                    '  TMP_BASE_DIR="${TMPDIR}"\n'
                    "elif test -n \"${SCRATCH}\" -a -d \"${SCRATCH}\"; then\n"
                    '  TMP_BASE_DIR="${SCRATCH}"\n'
                    "else\n"
                    '  TMP_BASE_DIR="${TMPDIR:-/tmp/${USER}}"\n'
                    "fi\n"
                )
            }

        def tmp_dir(self, task_id):
            # The submit host must NOT create/resolve any tmp dir for snippets:
            # the concrete per-job path only exists on the compute node.
            raise AssertionError("tmp_dir must not be called for shell snippets")

    # resource_config is a read-only property on GlobalState; the backing
    # instance attribute is what init_resource uses.
    monkeypatch.setattr(context, "_resource_config", StubResourceConfig())

    parent_parameters = Parameters(
        resource="justus-jinja",
        queue="slurm-queue",
        slurm_parameters=SlurmParameters(
            nodes=1,
            tasks_per_node=2,
            mem="1G",
            time="0:10:00",
            job_name="template-job",
            output="/template/%j.out",
            error="/template/%j.err",
            startup_commands=["module load chem/orca"],
            chdir="/template/work",
        ),
    )
    parent = _slurm_registry("parent", parent_parameters)

    await submit_node(parent)

    parent_script = (
        workdir / parent.name / str(parent.id) / "slurm_script.sh"
    ).read_text()
    # Snippet embedded verbatim for runtime evaluation in the trap ...
    assert 'Test' not in parent_script  # sanity guard
    assert 'TMP_BASE_DIR="${TMPDIR}"' in parent_script
    assert 'TMP_BASE_DIR="${SCRATCH}"' in parent_script
    # ... guarded cp operating on the runtime-resolved value ...
    assert f'&& [ -d "${{TMP_BASE_DIR}}/{parent.id}" ]; then' in parent_script
    assert parent_script.count("TMP_BASE_DIR") >= 4
    # ... and the trap is still armed.
    assert "trap 'sync_data' SIGUSR1" in parent_script
    assert parent.status == TaskStatus.SLURM_QUEUED
    assert parent.job_id == "12346"
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
