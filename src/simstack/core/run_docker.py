from simstack.models import NodeRegistry
from simstack.core.definitions import TaskStatus
from simstack.core.context import context
import asyncio
import platform
import subprocess
import logging
import os
import locale

logger = logging.getLogger("DockerRunner")

async def run_docker(registry_entry: NodeRegistry) -> bool:
    resource = context.config.resource
    parameters = registry_entry.parameters


    program_config = context.resource_config.get_program(registry_entry.name)
    image= program_config.get("docker_image", None)
    if image is None:
        logger.error(f"Docker image for {registry_entry.name} not found for task_id {registry_entry.id}")
        registry_entry.status = TaskStatus.FAILED
        await context.db.save(registry_entry)
        return False
    docker_cmd = program_config.get("docker_cmd", None)
    if docker_cmd is None:
        docker_cmd = "docker"

    if docker_cmd == "apptainer":
        if not image.endswith(".sif") and not image.startswith("docker://"):
             image += ".sif"

    workdir = context.config.workdir
    host_simstack_toml = context.config.project_root / "simstack.toml"

    if docker_cmd == "docker":
        cmd = [
            "docker", "run",
            "-e", f"SIMSTACK_DB_DATABASE={context.config.db_name}",
            "-e", f"SIMSTACK_DB_TEST_DATABASE={context.config.db_name}",
            "-e", f"SIMSTACK_DB_CONNECTION_STRING={context.config.connection_string}",
            "-v", f"{workdir}:/root/simstack",
            "-v", f"{host_simstack_toml}:/app/simstack.toml",
            image,
            "--node-id", str(registry_entry.id), "--resource", str(resource), "--project-root", "/app", "--in-docker"
        ]
    elif docker_cmd == "apptainer":
        cmd = [
            "apptainer", "run",
            "--env", f"SIMSTACK_DB_DATABASE={context.config.db_name}",
            "--env", f"SIMSTACK_DB_TEST_DATABASE={context.config.db_name}",
            "--env", f"SIMSTACK_DB_CONNECTION_STRING={context.config.connection_string}",
            "--bind", f"{workdir}:/root/simstack",
            "--bind", f"{host_simstack_toml}:/app/simstack.toml",
            image,
            "--node-id", str(registry_entry.id),
            "--resource", str(resource),
            "--project-root", "/app",
            "--in-docker",
        ]
    else:
        logger.error(f"Unsupported command {docker_cmd} for task_id={registry_entry.id}")
        registry_entry.status = TaskStatus.FAILED
        await context.db.save(registry_entry)
        return False

    # Use platform specific flags to ensure the process survives if runner is killed
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=True if platform.system() != "Windows" else False
        )

        stdout_b, stderr_b = await process.communicate()

        enc = locale.getpreferredencoding(False) or "utf-8"
        stdout = (stdout_b or b"").decode(enc, errors="replace").strip()
        stderr = (stderr_b or b"").decode(enc, errors="replace").strip()

        if process.returncode != 0:
            logger.error(
                "docker run failed for task_id=%s rc=%s stderr=%s stdout=%s cmd=%s",
                registry_entry.id, process.returncode, stderr, stdout, cmd
            )
            registry_entry.status = TaskStatus.FAILED
            await context.db.save(registry_entry)
            return False

        if stdout:
            # For `docker run` without -d, stdout is the actual output of the command
            logger.info("Docker container output for task_id=%s: %s", registry_entry.id, stdout)
        else:
            logger.info("Docker container spawned for task_id=%s", registry_entry.id)
        if stderr:
            # Some Docker setups warn on stderr even on success
            logger.warning("docker run stderr for task_id=%s: %s", registry_entry.id, stderr)

        return True
    except Exception as e:
        logger.exception(f"fatal error in running docker task_id: {registry_entry.id} {str(e)}")
        registry_entry.status = TaskStatus.FAILED
        await context.db.save(registry_entry)
        return False
