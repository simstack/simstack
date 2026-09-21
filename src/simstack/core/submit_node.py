import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry
from simstack.models.parameters import SlurmParameters
from simstack.util.sanitized_output import sanitized_tail
from simstack.util.submit_to_watchdog import submit_to_watchdog
import logging

logger = logging.getLogger("submit_node")

SALLOC_GRANT_WAIT_SECONDS = 300


async def _persist_submission_failure(
    registry_entry: NodeRegistry, error: str
) -> None:
    """Fail an owned submission without rolling a started task backward."""
    if registry_entry.status != TaskStatus.SLURM_QUEUED:
        registry_entry.status = TaskStatus.FAILED
        registry_entry.error = error
        await context.db.save(registry_entry)
        return

    collection = context.db.get_collection(NodeRegistry)
    updated = await collection.find_one_and_update(
        {
            "_id": registry_entry.id,
            "status": TaskStatus.SLURM_QUEUED.value,
        },
        {
            "$set": {
                "status": TaskStatus.FAILED.value,
                "error": error,
            }
        },
    )
    if updated is not None:
        registry_entry.status = TaskStatus.FAILED
        registry_entry.error = error


def make_executable(file_path: str | os.PathLike[str]) -> None:
    # Get current permissions
    current_permissions = os.stat(file_path).st_mode

    # Add executable bit for user, group and others
    executable_mode = current_permissions | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

    # Apply new permissions
    os.chmod(file_path, executable_mode)


def parse_salloc_job_id(output: str) -> str | None:
    match = re.search(r"Granted job allocation (\d+)", output)
    return match.group(1) if match else None


def wait_for_salloc_job_id(
    log_path: Path, proc: subprocess.Popen, timeout: float
) -> str:
    deadline = time.monotonic() + timeout
    while True:
        text = (
            log_path.read_text(encoding="utf-8", errors="replace")
            if log_path.exists()
            else ""
        )
        job_id = parse_salloc_job_id(text)
        if job_id:
            return job_id
        if proc.poll() is not None:
            raise RuntimeError(
                "salloc exited before granting an allocation "
                f"(returncode={proc.returncode}): {text}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"salloc did not grant a job id within {timeout}s: {text}"
            )
        time.sleep(0.2)


@dataclass
class PreparedSlurmJob:
    task_id: object
    work_dir: Path
    script_path: str
    command_script_path: str
    slurm_parameters: SlurmParameters
    database_env: dict
    connection_string: str | None


async def _prepare_slurm_job(registry_entry: NodeRegistry) -> PreparedSlurmJob | None:
    task_id = registry_entry.id
    base_path = context.config.project_root

    python_path = os.pathsep.join(
        dict.fromkeys(
            [str(base_path)]
            + [str(path) for path in context.config.python_paths]
        )
    )
    work_dir = context.config.workdir / registry_entry.name / str(registry_entry.id)
    job_name = registry_entry.name + "." + str(registry_entry.id)

    logger.info(f"task_id: {task_id} workdir {work_dir} python path {python_path}")

    original_slurm_parameters = registry_entry.parameters.slurm_parameters
    if original_slurm_parameters is None:
        logger.error(f"Task task_id: {task_id} has no slurm parameters -- failing")
        registry_entry.status = TaskStatus.FAILED
        await context.db.save(registry_entry)
        return None
    slurm_parameters = original_slurm_parameters.model_copy(deep=True)
    slurm_parameters.output = f"{work_dir}/%j.out"
    slurm_parameters.error = f"{work_dir}/%j.err"
    slurm_parameters.job_name = f"{job_name}"
    slurm_parameters.chdir = str(work_dir)

    slurm_parameters.startup_commands.append("source ~/.bashrc")
    slurm_parameters.startup_commands.append(f"{context.config.environment_start}")
    slurm_parameters.startup_commands.append(
        f"export PYTHONPATH={python_path}:$PYTHONPATH"
    )

    sync_data_start = """# 1. Define the rsync function
        sync_data() {
            echo "=== Time limit approaching! Starting rsync at $(date) ==="

            # Run rsync safely 
        """
    sync_data_end = """
            echo "=== Rsync completed at $(date) ==="
            exit 0
        }        
        """

    resource_value = registry_entry.parameters.resource
    task_resource = (
        getattr(resource_value, "__dict__", {}).get("value")
        or str(resource_value)
    )
    selected_resource = (
        str(context.config.resource) if task_resource == "self" else task_resource
    )
    if context.resource_config is not None:
        try:
            program_config = context.resource_config.get_program(
                registry_entry.name, resource=selected_resource
            ) or {}
        except ValueError:
            program_config = {}
    else:
        program_config = {}

    logger.info(
        "task_id: %s selected resource %s program %s",
        task_id,
        selected_resource,
        registry_entry.name,
    )
    if registry_entry.parameters.in_docker and not program_config.get(
        "docker_image"
    ):
        registry_entry.status = TaskStatus.FAILED
        registry_entry.error = (
            f"Docker image for {registry_entry.name} not found "
            f"(resource={selected_resource})"
        )
        logger.error("Task task_id: %s %s", task_id, registry_entry.error)
        await context.db.save(registry_entry)
        return None
    if program_config.get("use_tmp", False):
        tmp_dir = context.resource_config.tmp_dir(registry_entry.id)

        if not tmp_dir.exists() or not tmp_dir.is_dir():
            logger.error(
                f"Task task_id: {task_id} tmp_dir {tmp_dir} does not exist or is not a directory -- failing")
            registry_entry.status = TaskStatus.FAILED
            await context.db.save(registry_entry)
            return None

        full_sync_data = sync_data_start + f'cp -a {tmp_dir}/. {work_dir}/recovery\n' + sync_data_end

        logger.info(f"task_id: {task_id} full_sync_data {full_sync_data}")
        slurm_parameters.startup_commands.append(full_sync_data)
        slurm_parameters.startup_commands.append("trap 'sync_data' SIGUSR1")

    slurm_parameters.startup_commands.append(
        f"uv run --directory {base_path} run_node --node-id {registry_entry.id} "
        f"--resource {selected_resource} --project-root {base_path} &"
    )
    slurm_parameters.startup_commands.append("wait")
    slurm_parameters.signal = "B:SIGUSR1@60"

    os.makedirs(work_dir, exist_ok=True)
    script_path = os.path.join(work_dir, "slurm_script.sh")
    command_script_path = script_path
    if getattr(context.config, "docker", False):
        command_script_path = os.path.join(
            context.config.external_workdir,
            registry_entry.name,
            str(registry_entry.id),
            "slurm_script.sh",
        )

    connection_string = getattr(context.config, "connection_string", None)
    db_name = getattr(context.config, "db_name", None)
    database_env = {
        **(
            {"SIMSTACK_DB_CONNECTION_STRING": str(connection_string)}
            if connection_string is not None
            else {}
        ),
        **(
            {"SIMSTACK_DB_DATABASE": str(db_name)}
            if db_name is not None
            else {}
        ),
    }
    return PreparedSlurmJob(
        task_id=task_id,
        work_dir=work_dir,
        script_path=script_path,
        command_script_path=command_script_path,
        slurm_parameters=slurm_parameters,
        database_env=database_env,
        connection_string=connection_string,
    )


async def submit_node(registry_entry: NodeRegistry) -> bool:
    """Submit a node to the SLURM queue"""
    task_id = registry_entry.id
    try:
        logger.info(f"Submitting task_id: {task_id} to SLURM queue")
        prep = await _prepare_slurm_job(registry_entry)
        if prep is None:
            return False

        slurm_script = prep.slurm_parameters.to_sbatch_header()
        logger.info(f"task_id: {task_id} workdir {prep.work_dir} python path")
        with open(prep.script_path, "w") as f:
            f.write(slurm_script)

        make_executable(prep.script_path)
        registry_entry.status = TaskStatus.SLURM_QUEUED
        await context.db.save(registry_entry)
        if context.config.docker:
            job_id = "slurm_" + str(registry_entry.id)
            queue_dir = context.config.workdir / "queue"
            result = submit_to_watchdog(
                f"/usr/bin/sbatch {prep.command_script_path}",
                job_id,
                queue_dir,
                env=prep.database_env or None,
            )
        else:
            result = subprocess.run(
                f"/usr/bin/sbatch {prep.script_path}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,  # Add timeout to prevent hanging
                env={**os.environ, **prep.database_env},
            )

        stdout = sanitized_tail(result.stdout, prep.connection_string)
        stderr = sanitized_tail(result.stderr, prep.connection_string)
        logger.info(
            "submitting job task_id: %s returns: %s stdout=%s stderr=%s",
            task_id,
            result.returncode,
            stdout,
            stderr,
        )
        if result.returncode == 0:
            match = re.search(r"Submitted batch job (\d+)", stdout)
            if match:
                job_id = match.group(1)
                logger.info(
                    f"task_id: {task_id} job successfully submitted with job_id: {job_id}"
                )
                registry_entry.job_id = job_id
                collection = context.db.get_collection(NodeRegistry)
                await collection.update_one(
                    {"_id": registry_entry.id},
                    {"$set": {"job_id": job_id}},
                )
            else:
                logger.warning(
                    "task_id: %s job submitted but could not extract job_id from output: %s",
                    task_id,
                    stdout,
                )
        else:
            logger.error(
                "error submitting job for task_id: %s return code: %s stdout: %s stderr: %s",
                task_id,
                result.returncode,
                stdout,
                stderr,
            )
            error = sanitized_tail(
                f"sbatch failed with return code {result.returncode}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}",
                prep.connection_string,
            )
            await _persist_submission_failure(registry_entry, error)
            return False
        return True
    except Exception as e:
        error = sanitized_tail(
            str(e), getattr(context.config, "connection_string", None)
        ) or type(e).__name__
        logger.error("fatal error in submitting task_id: %s %s", task_id, error)
        await _persist_submission_failure(registry_entry, error)
        return False


async def submit_salloc(registry_entry: NodeRegistry) -> bool:
    """Allocate a compute node with salloc and run the node under srun.

    The salloc process is left running so the allocation is held for the
    master's walltime. Killing it releases the node.
    """
    task_id = registry_entry.id
    proc = None
    try:
        logger.info(f"Submitting task_id: {task_id} to salloc queue")
        prep = await _prepare_slurm_job(registry_entry)
        if prep is None:
            return False

        slurm_script = prep.slurm_parameters.to_shell_script(sbatch_header=False)
        with open(prep.script_path, "w") as f:
            f.write(slurm_script)
        make_executable(prep.script_path)

        command = prep.slurm_parameters.to_salloc_command(prep.command_script_path)
        log_path = prep.work_dir / "salloc.log"
        registry_entry.status = TaskStatus.SLURM_QUEUED
        await context.db.save(registry_entry)

        logger.info("task_id: %s salloc command: %s", task_id, command)
        log_file = open(log_path, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, **prep.database_env},
            )
        finally:
            log_file.close()

        job_id = wait_for_salloc_job_id(
            log_path, proc, SALLOC_GRANT_WAIT_SECONDS
        )
        logger.info(
            f"task_id: {task_id} salloc granted job_id: {job_id} pid: {proc.pid}"
        )
        registry_entry.job_id = job_id
        collection = context.db.get_collection(NodeRegistry)
        await collection.update_one(
            {"_id": registry_entry.id},
            {"$set": {"job_id": job_id}},
        )
        return True
    except Exception as e:
        if proc is not None and proc.poll() is None:
            proc.terminate()
        error = sanitized_tail(
            str(e), getattr(context.config, "connection_string", None)
        ) or type(e).__name__
        logger.error("fatal error in salloc for task_id: %s %s", task_id, error)
        await _persist_submission_failure(registry_entry, error)
        return False
