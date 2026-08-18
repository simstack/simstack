import asyncio
import locale
import logging
import platform
import re
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
_DOCKER_CIDFILE_NAME = ".docker_cid"
_SIGKILL_RC = 137
_SIGSEGV_RC = 139
_SLURM_MEMORY_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([MGmg])B?$")


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _slurm_task_count(slurm: object) -> int:
    tasks = _positive_int(getattr(slurm, "tasks", None))
    if tasks is not None:
        return tasks
    tasks_per_node = _positive_int(getattr(slurm, "tasks_per_node", None))
    if tasks_per_node is not None:
        return tasks_per_node
    return 1


def docker_cpu_limit(slurm: object | None) -> int | None:
    """Docker/Apptainer CPU count: cpus_per_task * tasks (or tasks_per_node)."""
    if slurm is None:
        return None

    cpus_per_task = _positive_int(getattr(slurm, "cpus_per_task", None))
    tasks = _positive_int(getattr(slurm, "tasks", None))
    tasks_per_node = _positive_int(getattr(slurm, "tasks_per_node", None))
    if cpus_per_task is None and tasks is None and tasks_per_node is None:
        return None

    return (cpus_per_task or 1) * _slurm_task_count(slurm)


def _parse_slurm_memory(value: object) -> tuple[float, str] | None:
    if not isinstance(value, str):
        return None
    match = _SLURM_MEMORY_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    return float(match.group(1)), match.group(2).lower()


def _format_container_memory(amount: float, unit: str, *, uppercase: bool) -> str:
    formatted_amount = str(int(amount)) if amount == int(amount) else str(amount)
    formatted_unit = unit.upper() if uppercase else unit.lower()
    return f"{formatted_amount}{formatted_unit}"


def docker_memory_limit(slurm: object | None, *, uppercase: bool = False) -> str | None:
    """Container memory from Slurm ``mem``, or ``mem_per_cpu`` times CPU count."""
    if slurm is None:
        return None

    mem = _parse_slurm_memory(getattr(slurm, "mem", None))
    if mem is not None:
        amount, unit = mem
        return _format_container_memory(amount, unit, uppercase=uppercase)

    mem_per_cpu = _parse_slurm_memory(getattr(slurm, "mem_per_cpu", None))
    if mem_per_cpu is None:
        return None

    cpu_count = docker_cpu_limit(slurm) or 1
    amount, unit = mem_per_cpu
    return _format_container_memory(amount * cpu_count, unit, uppercase=uppercase)


def container_resource_args(docker_cmd: str, slurm: object | None) -> list[str]:
    """Runtime flags that pin CPU and memory for docker or apptainer."""
    args: list[str] = []
    cpu_limit = docker_cpu_limit(slurm)
    if cpu_limit is not None:
        args.extend(["--cpus", str(cpu_limit)])

    memory_limit = docker_memory_limit(slurm, uppercase=docker_cmd == "apptainer")
    if memory_limit is not None:
        args.extend(["--memory", memory_limit])
    return args


def docker_cidfile_path(task_dir: Path | str) -> Path:
    return Path(task_dir) / _DOCKER_CIDFILE_NAME


def prepare_docker_cidfile(task_dir: Path | str) -> Path:
    """Return a cidfile path Docker can create. Docker errors if the file already exists."""
    cidfile = docker_cidfile_path(task_dir)
    cidfile.unlink(missing_ok=True)
    return cidfile


def read_container_id(cidfile: Path | str) -> str | None:
    try:
        container_id = Path(cidfile).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return container_id or None


def inspect_docker_oomkilled(container_id: str) -> bool | None:
    """Return Docker ``State.OOMKilled``, or None if inspect fails."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.OOMKilled}}", container_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def format_container_failure_error(
    returncode: int | None,
    *,
    oom_killed: bool | None = None,
    memory_limit: str | None = None,
    docker_cmd: str = "docker",
) -> str:
    """Human-readable task error for a dead docker/apptainer container."""
    rc = "unknown" if returncode is None else returncode
    oom_suffix = ""
    if oom_killed is True:
        oom_suffix = ", OOMKilled=true"
    elif oom_killed is False:
        oom_suffix = ", OOMKilled=false"

    if memory_limit:
        mem_hint = f"{docker_cmd} --memory was {memory_limit}."
    else:
        mem_hint = (
            f"{docker_cmd} --memory was not set (Slurm mem/mem_per_cpu unset). "
            "Increase mem or mem_per_cpu on the job."
        )

    if oom_killed is True:
        return (
            f"Container killed (exit {rc}{oom_suffix}). Likely out of memory. {mem_hint}"
        )
    if returncode in (_SIGKILL_RC, _SIGSEGV_RC):
        return (
            f"Container killed (exit {rc}{oom_suffix}). Likely OOM / SIGKILL. {mem_hint}"
        )
    extra = f" {docker_cmd} --memory was {memory_limit}." if memory_limit else ""
    return f"Container failed (exit {rc}{oom_suffix}).{extra}".rstrip()


def _container_oom_killed(
    docker_cmd: str, cidfile: Path | None
) -> bool | None:
    if docker_cmd != "docker" or cidfile is None:
        return None
    container_id = read_container_id(cidfile)
    if not container_id:
        return None
    return inspect_docker_oomkilled(container_id)


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
    task_dir = ensure_host_task_workdir(workdir, registry_entry.name, str(registry_entry.id))
    cidfile: Path | None = None
    if docker_cmd == "docker":
        cidfile = prepare_docker_cidfile(task_dir)
    host_simstack_toml = context.config.project_root / "simstack.toml"
    connection_string = context.config.connection_string
    docker_net_args: list[str] = []

    if docker_cmd == "docker" and _mongo_host_is_loopback(connection_string):
        connection_string, docker_net_args = _docker_loopback_mongo_args(connection_string)

    slurm_parameters = getattr(registry_entry.parameters, "slurm_parameters", None)
    resource_args = container_resource_args(docker_cmd, slurm_parameters)
    if resource_args:
        logger.info(
            "task_id=%s applying container resource limits from slurm_parameters: %s",
            registry_entry.id,
            shlex.join(resource_args),
        )

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
        if cidfile is None:
            raise RuntimeError("docker cidfile was not prepared")
        cmd = [
            "docker", "run",
            "--cidfile", str(cidfile),
            *docker_net_args,
            *resource_args,
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
            *resource_args,
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
        "starting docker for task_id: %s full docker command: %s",
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
            oom_killed = _container_oom_killed(docker_cmd, cidfile)
            memory_limit = docker_memory_limit(
                slurm_parameters, uppercase=docker_cmd == "apptainer"
            )
            registry_entry.error = format_container_failure_error(
                process.returncode,
                oom_killed=oom_killed,
                memory_limit=memory_limit,
                docker_cmd=docker_cmd,
            )
            logger.error(
                "docker run failed for task_id=%s rc=%s oom_killed=%s error=%s stderr=%s stdout=%s cmd=%s",
                registry_entry.id,
                process.returncode,
                oom_killed,
                registry_entry.error,
                stderr,
                stdout,
                cmd,
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
        if not getattr(registry_entry, "error", None):
            registry_entry.error = f"Failed to run {docker_cmd}: {e}"
        await context.db.save(registry_entry)
        return False
