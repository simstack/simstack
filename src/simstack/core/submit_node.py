from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from simstack.core.context import context
from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.models import NodeRegistry
from simstack.runner_runtime.environment import (
    sanitized_launcher_environment,
    sanitized_runner_environment,
    slurm_export_argument,
    worker_environment_allowlist_value,
)
from simstack.runner_runtime.config import runner_task_workdir
from simstack.runner_runtime.container_launch import container_launch
from simstack.runner_api import ExecutionRunnerClient


logger = logging.getLogger("submit_node")


def make_executable(file_path: str | os.PathLike[str]) -> None:
    os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _runner_mode() -> bool:
    return bool(context.runner_mode)


def _status_value(status: TaskStatus | str) -> str:
    return status.value if isinstance(status, TaskStatus) else str(status)


async def submit_node(
    registry_entry: NodeRegistry,
    *,
    claim_id: str | None = None,
    attempt: int | None = None,
    runner_api: ExecutionRunnerClient,
) -> bool:
    """Submit an API-owned node through an explicit Slurm launch contract."""

    task_id = str(registry_entry.id)
    api_mode = _runner_mode()
    if not api_mode:
        raise RuntimeError(
            "Slurm runner submission requires initialized API-only runner mode"
        )
    if not claim_id or attempt is None or attempt < 1:
        raise RuntimeError("Slurm runner submission requires claim_id and attempt")

    try:
        logger.info("Submitting task_id=%s to Slurm", task_id)
        base_path = Path(context.config.project_root)
        api_environment = {
            "SIMSTACK_SERVER_URL": runner_api.server_url,
            "SIMSTACK_RUNNER_TOKEN": runner_api.runner_token,
            "SIMSTACK_TASK_ID": task_id,
            "SIMSTACK_TASK_CLAIM_ID": claim_id,
            "SIMSTACK_TASK_ATTEMPT": str(attempt),
            "SIMSTACK_RUNNER_RESOURCE": str(context.config.resource),
            "SIMSTACK_PROJECT_ROOT": str(base_path),
            "SIMSTACK_RUNNER_WORKDIR": str(context.config.workdir),
        }
        python_path = os.pathsep.join(
            dict.fromkeys(str(path) for path in (base_path, *context.config.python_paths))
        )
        work_dir = runner_task_workdir(Path(context.config.workdir), task_id)
        work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        work_dir.chmod(0o700)
        job_name = f"simstack-{task_id}"

        original_slurm_parameters = registry_entry.parameters.slurm_parameters
        if original_slurm_parameters is None:
            raise RuntimeError(f"Task {task_id} has no Slurm parameters")
        slurm_parameters = original_slurm_parameters.model_copy(deep=True)
        if slurm_parameters.startup_commands:
            raise RuntimeError(
                "Task-provided Slurm startup_commands are forbidden in API runner mode"
            )
        if slurm_parameters.export is not None:
            raise RuntimeError(
                "Task-provided Slurm export is forbidden in API runner mode"
            )
        slurm_parameters.output = f"{work_dir}/%j.out"
        slurm_parameters.error = f"{work_dir}/%j.err"
        slurm_parameters.job_name = job_name
        slurm_parameters.chdir = str(work_dir)

        slurm_parameters.startup_commands.extend(
            (
                "umask 077",
                ': "${SIMSTACK_SERVER_URL:?SIMSTACK_SERVER_URL is required}"',
                ': "${SIMSTACK_RUNNER_TOKEN:?SIMSTACK_RUNNER_TOKEN is required}"',
                "readonly SIMSTACK_SERVER_URL SIMSTACK_RUNNER_TOKEN "
                "SIMSTACK_TASK_ID SIMSTACK_TASK_CLAIM_ID SIMSTACK_TASK_ATTEMPT "
                "SIMSTACK_RUNNER_RESOURCE SIMSTACK_PROJECT_ROOT "
                "SIMSTACK_RUNNER_WORKDIR",
            )
        )
        if context.config.environment_start:
            slurm_parameters.startup_commands.append(
                str(context.config.environment_start)
            )
        slurm_parameters.startup_commands.append(
            f"export PYTHONPATH={shlex.quote(python_path)}"
        )

        sync_data_start = """# 1. Define the rsync function
sync_data() {
    echo "=== Time limit approaching! Starting rsync at $(date) ==="
"""
        sync_data_end = """
    echo "=== Rsync completed at $(date) ==="
    exit 0
}
"""

        profile = getattr(registry_entry, "runtime_profile", None)
        adapter = context.runner_adapter.slurm(registry_entry.name, **({"runtime_profile": profile} if profile else {}))
        environment_allowlist = adapter.boundary.environment_allowlist
        api_environment["SIMSTACK_WORKER_ENVIRONMENT_ALLOWLIST"] = (
            worker_environment_allowlist_value(environment_allowlist)
        )
        worker_environment = sanitized_runner_environment(
            api_environment,
            allowed_environment_names=environment_allowlist,
            require_allowed=False,
            workflow_bootstrap=bool(getattr(registry_entry, "workflow_revision", None)),
        )
        submission_environment = sanitized_launcher_environment(
            api_environment,
            allowed_environment_names=environment_allowlist,
            require_allowed=False,
            workflow_bootstrap=bool(getattr(registry_entry, "workflow_revision", None)),
        )
        slurm_parameters.startup_commands.extend(
            f': "${{{name}:?{name} is required by worker_environment_allowlist}}"'
            for name in environment_allowlist
        )
        if adapter.use_tmp:
            tmp_dir = adapter.task_tmp_dir(registry_entry.id)
            if not tmp_dir.is_dir():
                raise RuntimeError(f"Task {task_id} tmp_dir {tmp_dir} is unavailable")
            full_sync_data = (
                sync_data_start
                + f"cp -a {shlex.quote(str(tmp_dir))}/. "
                + f"{shlex.quote(str(work_dir))}/recovery\n"
                + sync_data_end
            )
            slurm_parameters.startup_commands.append(full_sync_data)
            slurm_parameters.startup_commands.append("trap 'sync_data' SIGUSR1")

        from simstack.runner_runtime.workflow_installation import packaged_worker_command

        worker_command = list(packaged_worker_command(adapter.guarded_worker_command, registry_entry, workdir=Path(context.config.workdir)))
        worker_command.extend(
            (
                "--node-id",
                task_id,
                "--resource",
                str(context.config.resource),
                "--project-root",
                str(base_path),
                "--claim-id",
                claim_id,
                "--attempt",
                str(attempt),
            )
        )
        if registry_entry.parameters.in_docker:
            worker_command, container_environment = container_launch(
                registry_entry, claim_id=claim_id, attempt=attempt,
                runner_api=runner_api,
                config=context.config, execution_adapter=context.runner_adapter,
            )
            worker_environment = container_environment
            submission_environment.update(container_environment)
        isolated_environment = list(
            dict.fromkeys((*worker_environment, *environment_allowlist, "PYTHONPATH"))
        )
        isolated_assignments = [
            f'{name}="${{{name}}}"' for name in isolated_environment
        ]
        slurm_parameters.startup_commands.append(
            " ".join(
                (
                    "env",
                    "-i",
                    *isolated_assignments,
                    *(shlex.quote(item) for item in worker_command),
                )
            )
        )
        slurm_parameters.signal = "B:SIGUSR1@60"
        _validate_sbatch_arguments(slurm_parameters.to_sbatch_args())

        script_path = work_dir / "slurm_script.sh"
        script_path.write_text(slurm_parameters.to_sbatch_header(), encoding="utf-8")
        make_executable(script_path)

        export_argument = slurm_export_argument(
            worker_environment,
            extra_names=environment_allowlist,
        )
        sbatch_command = list(adapter.submit_command)
        sbatch_command.extend((export_argument, str(script_path)))
        # Publish the queued state before the scheduler can start the worker.
        # Its first task read must not race a later Retrieved -> Queued write.
        await runner_api.update_task_status(
            task_id,
            claim_id=claim_id,
            attempt=attempt,
            expected_status=_status_value(registry_entry.status),
            status=TaskStatus.SLURM_QUEUED.value,
            observed_at=datetime.now(UTC).isoformat(),
        )
        result = await asyncio.to_thread(
            subprocess.run,
            sbatch_command,
            capture_output=True,
            text=True,
            timeout=30,
            env=submission_environment,
            check=False,
        )

        logger.info(
            "Slurm submission task_id=%s returned rc=%s stdout=%s stderr=%s",
            task_id,
            result.returncode,
            result.stdout,
            result.stderr,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Slurm submission failed with code {result.returncode}: {result.stderr}"
            )
        match = re.search(r"Submitted batch job (\d+)", result.stdout)
        if match is None:
            raise RuntimeError(
                f"Slurm accepted task {task_id} but returned no job id: {result.stdout}"
            )
        job_id = match.group(1)

        await runner_api.update_task_status(
            task_id,
            claim_id=claim_id,
            attempt=attempt,
            # This claim-bound publication attaches scheduler metadata only;
            # Server preserves a worker's newer running/terminal task state.
            expected_status=TaskStatus.SLURM_QUEUED.value,
            status=TaskStatus.SLURM_QUEUED.value,
            observed_at=datetime.now(UTC).isoformat(),
            job_id=job_id,
        )
        return True
    except Exception:
        logger.exception("Fatal error submitting task_id=%s", task_id)
        raise


def _validate_sbatch_arguments(arguments: list[str]) -> None:
    for argument in arguments:
        if not argument or any(
            ord(character) < 32 or ord(character) == 127 for character in argument
        ):
            raise RuntimeError("Slurm SBATCH values cannot contain control characters")
