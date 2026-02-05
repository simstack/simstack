from simstack.models import NodeRegistry
from simstack.core.context import context
import asyncio
import platform
import subprocess
import logging
import os

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
        "-e", f"SIMSTACK_DB_TEST_DATABASE={context.config.db_name}",
        "-e", f"SIMSTACK_DB_CONNECTION_STRING={context.config.connection_string}",
        "-v", f"{workdir}:/work/simstack",
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
    logger.info(f"Spawned detached docker container for task_id: {registry_entry.id} with PID: {process.pid}")

    async def log_stream(stream, stream_name):
        while True:
            line = await stream.readline()
            if not line:
                break
            logger.info(f"[{registry_entry.id}] {stream_name}: {line.decode().rstrip()}")

    # Create tasks to read stdout and stderr
    asyncio.create_task(log_stream(process.stdout, "stdout"))
    asyncio.create_task(log_stream(process.stderr, "stderr"))

    return True
    pass