import asyncio
import locale
import logging
import platform
import shlex
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry

logger = logging.getLogger("DockerRunner")

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_DOCKER_WORKDIR = "/root/simstack"


def ensure_host_task_workdir(workdir: Path | str, node_name: str, node_id: str) -> Path:
    """Create the task directory as the host user before Docker/Apptainer runs.

    Containers default to root. If they mkdir ``{workdir}/{node_name}/{id}`` on a
    bind-mounted volume, the node-type directory becomes root-owned mode 755 and
    the runner user can no longer create later task dirs.
    """
    path = Path(workdir) / node_name / str(node_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _mongo_host_is_loopback(connection_string: str | None) -> bool:
    if not connection_string:
        return False
    host = (urlparse(connection_string).hostname or "").lower()
    return host in _LOCAL_HOSTS


def _rewrite_mongo_host(connection_string: str, new_host: str) -> str:
    """Replace the Mongo URI host while preserving userinfo, port, path, and query."""
    parsed = urlparse(connection_string)
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    port = parsed.port
    hostport = new_host if port is None else f"{new_host}:{port}"
    return urlunparse(
        (parsed.scheme, f"{userinfo}{hostport}", parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


def _docker_loopback_mongo_args(connection_string: str) -> tuple[str, list[str]]:
    """
    Make loopback Mongo reachable from inside `docker run`.

    On Linux, mongod often binds 127.0.0.1 only, so bridge networking cannot
    reach it — use host networking. On Docker Desktop (Windows/macOS), rewrite
    to host.docker.internal instead.
    """
    if platform.system() == "Linux":
        logger.info("Mongo URI uses loopback; using --network host for docker run")
        return connection_string, ["--network", "host"]

    rewritten = _rewrite_mongo_host(connection_string, "host.docker.internal")
    logger.info(
        "Mongo URI uses loopback; rewriting host to host.docker.internal for docker run"
    )
    return rewritten, ["--add-host", "host.docker.internal:host-gateway"]


def _reload_resource_config() -> None:
    """Re-read config.toml so docker_image assignments from git pull are visible."""
    resource_config = context.resource_config
    if resource_config is None:
        return
    resource_config.reload()
    logger.info("Reloaded resource config from %s", resource_config._config_path)


def _docker_program_config(registry_entry: NodeRegistry) -> tuple[dict, str]:
    """Look up ``[resource.program.<node>]`` after config.toml has been reloaded.

    Context may be ``self`` while the task targets ``local`` (common in tests);
    images live under ``[local.program]``.
    """
    task_resource = str(registry_entry.parameters.resource)
    lookup_resource = "local" if task_resource == "self" else task_resource
    program_config = context.resource_config.get_program(
        registry_entry.name, resource=lookup_resource
    )
    if not program_config and lookup_resource != str(context.config.resource):
        # Fall back to context resource (e.g. runner already bound to "local").
        program_config = context.resource_config.get_program(registry_entry.name)
    return program_config, lookup_resource


async def run_docker(registry_entry: NodeRegistry) -> bool:
    _reload_resource_config()
    program_config, lookup_resource = _docker_program_config(registry_entry)

    image = program_config.get("docker_image", None)
    if image is None:
        logger.error(f"Docker image for {registry_entry.name} not found for task_id: {registry_entry.id} (looked up resource={lookup_resource})")
        registry_entry.status = TaskStatus.FAILED
        registry_entry.error = f"Docker image for {registry_entry.name} not found (resource={lookup_resource})"
        await context.db.save(registry_entry)
        return False
    docker_cmd = program_config.get("docker_cmd", None)
    if docker_cmd is None:
        docker_cmd = "docker"

    if docker_cmd == "apptainer":
        if not image.endswith(".sif") and not image.startswith("docker://"):
             image += ".sif"

    workdir = context.config.workdir
    ensure_host_task_workdir(workdir, registry_entry.name, str(registry_entry.id))
    host_simstack_toml = context.config.project_root / "simstack.toml"
    connection_string = context.config.connection_string
    docker_net_args: list[str] = []

    if docker_cmd == "docker" and _mongo_host_is_loopback(connection_string):
        connection_string, docker_net_args = _docker_loopback_mongo_args(connection_string)

    # Prefer the task resource inside the container (self -> local for image/workdir lookups).
    container_resource = lookup_resource
    # Dev convenience: overlay local simstack package so in-progress host fixes apply in-container
    # without rebuilding the image (psi4 image installs simstack from git).
    # TODO this is a bit dangerous because you dont know which simstack was baked in
    # host_simstack_pkg = context.config.project_root / "simstack" / "src" / "simstack"
    # simstack_mount_args: list[str] = []
    # if host_simstack_pkg.is_dir():
    #     simstack_mount_args = [
    #         "-v",
    #         f"{host_simstack_pkg}:/opt/conda/lib/python3.12/site-packages/simstack",
    #     ]
    #     logger.info(
    #         "Mounting local simstack package into container: %s", host_simstack_pkg
    #     )

    if docker_cmd == "docker":
        cmd = [
            "docker", "run",
            *docker_net_args,
            "-e", f"SIMSTACK_DB_DATABASE={context.config.db_name}",
            "-e", f"SIMSTACK_DB_TEST_DATABASE={context.config.db_name}",
            "-e", f"SIMSTACK_DB_CONNECTION_STRING={connection_string}",
            "-v", f"{workdir}:{_DOCKER_WORKDIR}",
            "-v", f"{host_simstack_toml}:/app/simstack.toml",
            *simstack_mount_args,
            image,
            "--node-id", str(registry_entry.id),
            "--resource", container_resource,
            "--project-root", "/app",
            "--in-docker",
        ]
    elif docker_cmd == "apptainer":
        bind_args = [
            "--bind", f"{workdir}:{_DOCKER_WORKDIR}",
            "--bind", f"{host_simstack_toml}:/app/simstack.toml",
        ]
        if host_simstack_pkg.is_dir():
            bind_args.extend([
                "--bind",
                f"{host_simstack_pkg}:/opt/conda/lib/python3.12/site-packages/simstack",
            ])
        cmd = [
            "apptainer", "run",
            "--env", f"SIMSTACK_DB_DATABASE={context.config.db_name}",
            "--env", f"SIMSTACK_DB_TEST_DATABASE={context.config.db_name}",
            "--env", f"SIMSTACK_DB_CONNECTION_STRING={connection_string}",
            *bind_args,
            image,
            "--node-id", str(registry_entry.id),
            "--resource", container_resource,
            "--project-root", "/app",
            "--in-docker",
        ]
    else:
        logger.error(f"Unsupported command {docker_cmd} for task_id={registry_entry.id}")
        registry_entry.status = TaskStatus.FAILED
        await context.db.save(registry_entry)
        return False

    # Mark queued just before launch so the UI / pollers see the handoff.
    registry_entry.status = TaskStatus.SLURM_QUEUED
    await context.db.save(registry_entry)
    logger.info(
        "task_id=%s status set to %s before docker launch",
        registry_entry.id,
        TaskStatus.SLURM_QUEUED,
    )

    # Sanitize cmd for logging by replacing connection string with placeholder
    sanitized_cmd = []
    skip_next = False
    for i, arg in enumerate(cmd):
        if skip_next:
            sanitized_cmd.append("***REDACTED***")
            skip_next = False
        elif arg in ("-e", "--env") and i + 1 < len(cmd) and cmd[i + 1].startswith("SIMSTACK_DB_CONNECTION_STRING="):
            sanitized_cmd.append(arg)
            skip_next = True
        elif arg.startswith("SIMSTACK_DB_CONNECTION_STRING="):
            sanitized_cmd.append("SIMSTACK_DB_CONNECTION_STRING=***REDACTED***")
        else:
            sanitized_cmd.append(arg)

    logger.info(
        "task_id=%s full docker command: %s",
        registry_entry.id,
        shlex.join(sanitized_cmd),
    )

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
