from simstack.models import NodeRegistry
from simstack.core.context import context
import asyncio
import platform
import subprocess
import logging
import os
import locale

logger = logging.getLogger("DockerRunner")

async def run_docker(registry_entry: NodeRegistry):
    resource = context.config.resource
    parameters = registry_entry.parameters

    if parameters.docker_image:
        image = parameters.docker_image
    else:
        image = context.config.docker_image

    workdir = str(context.config.workdir)

    cmd = [
        "docker", "run", "-d",
        "-e", f"SIMSTACK_DB_DATABASE={context.config.db_name}",
        "-e", f"SIMSTACK_DB_TEST_DATABaASE={context.config.db_name}",
        "-e", f"SIMSTACK_DB_CONNECTION_STRING={context.config.connection_string}",
        "-v", f"{workdir}:/root/simstack",
        image,
        "uv", "run", "run_node", "--node-id", str(registry_entry.id), "--resource", str(resource)
    ]

    # Use platform specific flags to ensure the process survives if runner is killed
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

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
        return False

    if stdout:
        # For `docker run -d`, stdout is usually the container id
        logger.info("Spawned docker container for task_id=%s: %s", registry_entry.id, stdout)
    if stderr:
        # Some Docker setups warn on stderr even on success
        logger.warning("docker run stderr for task_id=%s: %s", registry_entry.id, stderr)

    return True
