import os
import re
import stat
import subprocess
from pathlib import Path

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry
from simstack.util.sanitized_output import sanitized_tail
from simstack.util.submit_to_watchdog import submit_to_watchdog
import logging

logger = logging.getLogger("submit_node")


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


async def submit_node(registry_entry: NodeRegistry) -> bool:
    """Submit a node to the SLURM queue"""
    task_id = registry_entry.id
    try:
        logger.info(f"Submitting task_id: {task_id} to SLURM queue")
        # Implement SLURM submission logic here
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
        # write a slurm script that starts a python script "run_node.py" with the node id as an argument

        original_slurm_parameters = registry_entry.parameters.slurm_parameters
        if original_slurm_parameters is None:
            logger.error(f"Task task_id: {task_id} has no slurm parameters -- failing")
            registry_entry.status = TaskStatus.FAILED
            await context.db.save(registry_entry)
            return False
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
            program_config = context.resource_config.get_program(
                registry_entry.name, resource=selected_resource
            ) or {}
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
            return False
        if program_config.get("use_tmp", False):
            tmp_dir = context.resource_config.tmp_dir(registry_entry.id)

            if not tmp_dir.exists() or not tmp_dir.is_dir():
                logger.error(
                    f"Task task_id: {task_id} tmp_dir {tmp_dir} does not exist or is not a directory -- failing")
                registry_entry.status = TaskStatus.FAILED
                await context.db.save(registry_entry)
                return False

            full_sync_data = sync_data_start + f'cp -a {tmp_dir}/. {work_dir}/recovery\n' + sync_data_end

            logger.info(f"task_id: {task_id} full_sync_data {full_sync_data}")
            slurm_parameters.startup_commands.append(full_sync_data)
            slurm_parameters.startup_commands.append("trap 'sync_data' SIGUSR1")

        # ``run_node`` applies the task's independent ``in_docker`` flag inside
        # the Slurm allocation, using the selected resource's program config.
        slurm_parameters.startup_commands.append(
            f"uv run --directory {base_path} run_node --node-id {registry_entry.id} "
            f"--resource {selected_resource} --project-root {base_path} &"
        )
        slurm_parameters.startup_commands.append("wait")
        slurm_parameters.signal = "B:SIGUSR1@60"

        slurm_script = slurm_parameters.to_sbatch_header()
        # write the script to a file in the work_dir
        os.makedirs(work_dir, exist_ok=True)
        logger.info(f"task_id: {task_id} workdir {work_dir} python path {python_path}")
        script_path = os.path.join(work_dir, "slurm_script.sh")
        with open(script_path, "w") as f:
            f.write(slurm_script)

        make_executable(script_path)
        # submit the script to the slurm queue
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
        # Publish the queued state before sbatch. A very short job may update
        # the same record before the submission command returns.
        registry_entry.status = TaskStatus.SLURM_QUEUED
        await context.db.save(registry_entry)
        if context.config.docker:
            external_work_dir = (
                context.config.external_workdir
                / registry_entry.name
                / str(registry_entry.id)
            )
            job_id = "slurm_" + str(registry_entry.id)
            queue_dir = context.config.workdir / "queue"
            result = submit_to_watchdog(
                f"/usr/bin/sbatch {os.path.join(external_work_dir, 'slurm_script.sh')}",
                job_id,
                queue_dir,
                env=database_env or None,
            )
        else:
            result = subprocess.run(
                f"/usr/bin/sbatch {os.path.join(work_dir, 'slurm_script.sh')}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,  # Add timeout to prevent hanging
                env={**os.environ, **database_env},
            )

        stdout = sanitized_tail(result.stdout, connection_string)
        stderr = sanitized_tail(result.stderr, connection_string)
        logger.info(
            "submitting job task_id: %s returns: %s stdout=%s stderr=%s",
            task_id,
            result.returncode,
            stdout,
            stderr,
        )
        if result.returncode == 0:
            # Extract job ID using a regex pattern
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
                connection_string,
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
