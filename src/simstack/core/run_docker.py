import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.run_node_protocol import RESULT_PREFIX, parse_run_node_result
from simstack.models import NodeRegistry
from simstack.util.output_permissions import docker_harmonize_command
from simstack.util.sanitized_output import (
    sanitized_command,
    sanitized_tail,
)

logger = logging.getLogger("DockerRunner")

_DOCKER_HUB_LIBRARY_PREFIX = "docker.io/library/"
_pull_locks: dict[str, asyncio.Lock] = {}
# In-container alias for the host resource workdir (not scratch). Must already
# exist in typical images. Nested /tmp/simstack is hidden by Apptainer's default
# host-/tmp bind; do not overlay container /tmp.
CONTAINER_WORKDIR = "/mnt"
# Images built before CONTAINER_WORKDIR moved still initialize --in-docker at
# /tmp/simstack. Bind the host workdir there too; --no-mount tmp keeps the
# image mkdir visible.
LEGACY_CONTAINER_WORKDIR = "/tmp/simstack"
_DOCKER_CIDFILE_NAME = ".docker_cid"
_SIGKILL_RC = 137
_SIGSEGV_RC = 139
_SLURM_MEMORY_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([MGmg])B?$")


@dataclass(frozen=True)
class DockerRunResult:
    success: bool
    return_kind: str
    error: str | None = None


def docker_execution_name(api, execution) -> str:
    """Docker names identify one account's original external launch, not its PID."""
    account = api.requests.bootstrap_data["account_id"]
    identity = [api.requests.server_url.rstrip("/"), account, execution]
    return "simstack-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def docker_daemon_identity():
    """Bind absence observations to the daemon which accepted this execution."""
    result = subprocess.run(["docker", "info", "--format", "{{.ID}}"],
        capture_output=True, text=True, timeout=10)
    identity = result.stdout.strip()
    if result.returncode or not identity or identity == "<no value>":
        raise RuntimeError("Docker daemon identity is unavailable")
    return identity


def inspect_docker_execution(name):
    """Return exact execution evidence; daemon/query failure is never absence."""
    result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=10)
    if result.returncode:
        error = result.stderr.casefold()
        if "no such object:" in error or "no such container:" in error:
            return None
        raise RuntimeError("Docker execution observation is unavailable")
    try:
        value, = json.loads(result.stdout)
        state = value["State"]
        if (not isinstance(state, dict) or state.get("Status") not in {
                "created", "running", "paused", "restarting", "removing", "exited", "dead"}
                or type(state.get("ExitCode")) is not int or not re.fullmatch("[0-9a-f]{64}", value["Id"])):
            raise ValueError
        label = (value["Config"].get("Labels") or {}).get("simstack.execution")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Docker returned malformed execution evidence") from exc
    if label != name or value.get("Name") != "/" + name:
        raise ValueError("Docker container does not belong to this execution")
    return value


async def _persist_post_launch_failure(
    launch_entry: NodeRegistry, error: str, *, diagnostics=None, returncode: int | None = None,
    return_kind="exception", pending_failure=None,
) -> None:
    """Report the original launch; a child error or competing start wins on Server."""
    from simstack.runner_runtime.pending_execution_failures import record_failure
    termination = {
        "exit_code": returncode if returncode is not None and returncode >= 0 else None,
        "signal": -returncode if returncode is not None and returncode < 0 else None,
    }
    if pending_failure is None:
        record_failure(error, phase="container", execution=context.server.tasks.execution(launch_entry),
            server=context.server, resource=getattr(launch_entry, "_execution_resource", None), return_kind=return_kind, **termination)
    else:
        # Retain the already-known scientific cause before the failure POST or
        # receipt delivery can become the first authoritative Server outcome.
        pending_failure[1].update(error=sanitized_tail(error, None), return_kind=return_kind)
        try:
            record_failure(server=context.server,
                resource=getattr(launch_entry, "_execution_resource", None), **pending_failure[1])
        except OSError:
            logger.warning("Could not update task %s container failure cause", launch_entry.id, exc_info=True)
    from simstack.server_api.http_transport import RunnerAPIError
    while True:
        try:
            await context.server.tasks.fail_attempt(launch_entry, error, phase="container", diagnostics=diagnostics,
                return_kind=return_kind, **termination)
            break
        except RunnerAPIError as exc:
            if not getattr(launch_entry, "_execution_owner", None) or exc.status_code is not None and exc.status_code < 500:
                logger.warning("Container failure retained for task %s reconciliation", launch_entry.id, exc_info=True)
                break
            logger.warning("Local execution owner awaits its stopped-container outcome", exc_info=True)
            await asyncio.sleep(1)
        except Exception:
            logger.warning("Container failure retained for task %s reconciliation", launch_entry.id, exc_info=True)
            break


def launcher_owner_ids() -> tuple[int, int]:
    """Uid and gid of the process that launches the container."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if not callable(getuid) or not callable(getgid):
        raise ValueError("launcher uid and gid are required to harmonize task output")
    uid = getuid()
    gid = getgid()
    if uid is None or gid is None:
        raise ValueError("launcher uid and gid are required to harmonize task output")
    return int(uid), int(gid)


async def _harmonize_docker_task_dir(image: str, task_dir: Path, uid: int, gid: int) -> None:
    command = docker_harmonize_command(
        image=image, task_dir=task_dir, uid=uid, gid=gid
    )
    logger.info(
        "harmonizing task directory %s to %s:%s",
        task_dir,
        uid,
        gid,
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await process.communicate()
    if process.returncode != 0:
        detail = (stderr_b or stdout_b or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"harmonize task directory {task_dir} failed rc={process.returncode}: {detail}"
        )


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
    """Runtime flags that pin CPU and memory for docker.

    Apptainer ``--cpus`` / ``--memory`` need cgroups v2 unified mode, which HPC
    nodes (Justus) do not provide. Slurm already enforces the job's CPU and memory.
    """
    if docker_cmd == "apptainer":
        return []

    args: list[str] = []
    cpu_limit = docker_cpu_limit(slurm)
    if cpu_limit is not None:
        args.extend(["--cpus", str(cpu_limit)])

    memory_limit = docker_memory_limit(slurm, uppercase=False)
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


def _image_has_registry_host(image: str) -> bool:
    """True if the first path component looks like a registry (host:port or host.tld)."""
    if "/" not in image:
        return False
    first = image.split("/", 1)[0]
    return ":" in first or "." in first


def resolve_docker_pull_ref(image: str, docker_registry: str | None) -> str | None:
    """Registry ref to ``docker pull``, or None to skip (local / Hub library alias)."""
    if not image or image.endswith(".sif") or image.startswith("docker://"):
        return None

    name = image
    if name.startswith(_DOCKER_HUB_LIBRARY_PREFIX):
        name = name[len(_DOCKER_HUB_LIBRARY_PREFIX) :]
    elif name.startswith("docker.io/"):
        name = name[len("docker.io/") :]

    if _image_has_registry_host(name):
        return name

    if docker_registry:
        return f"{docker_registry.rstrip('/')}/{name}"
    return None


def _pull_lock(pull_ref: str) -> asyncio.Lock:
    lock = _pull_locks.get(pull_ref)
    if lock is None:
        lock = asyncio.Lock()
        _pull_locks[pull_ref] = lock
    return lock


async def pull_docker_image(image: str, docker_registry: str | None) -> None:
    """Pull ``image`` when a registry is known. Failure is logged; run still proceeds."""
    safe_image = sanitized_tail(image, None, limit=512)
    pull_ref = resolve_docker_pull_ref(image, docker_registry)
    if pull_ref is None:
        logger.debug(
            "Skipping docker pull for %s (no docker_registry / local tag)", safe_image
        )
        return

    safe_pull_ref = sanitized_tail(pull_ref, None, limit=512)

    async with _pull_lock(pull_ref):
        logger.info("docker pull %s", safe_pull_ref)
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "pull",
                pull_ref,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await process.communicate()
        except OSError as exc:
            logger.warning(
                "docker pull %s failed to start: %s",
                safe_pull_ref,
                sanitized_tail(str(exc), None),
            )
            return

        stdout = sanitized_tail(stdout_b, None)
        stderr = sanitized_tail(stderr_b, None)
        if process.returncode != 0:
            logger.warning(
                "docker pull %s failed rc=%s stderr=%s stdout=%s; using local image if present",
                safe_pull_ref,
                process.returncode,
                stderr,
                stdout,
            )
            return
        if stdout:
            logger.info("docker pull %s: %s", safe_pull_ref, stdout.splitlines()[-1])

        if pull_ref != image:
            try:
                tag_process = await asyncio.create_subprocess_exec(
                    "docker",
                    "tag",
                    pull_ref,
                    image,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                logger.warning(
                    "docker tag %s -> %s failed to start: %s",
                    safe_pull_ref,
                    safe_image,
                    sanitized_tail(str(exc), None),
                )
                return
            _, tag_err = await tag_process.communicate()
            if tag_process.returncode != 0:
                logger.warning(
                    "docker tag %s -> %s failed rc=%s stderr=%s",
                    safe_pull_ref,
                    safe_image,
                    tag_process.returncode,
                    sanitized_tail(tag_err, None),
                )
            else:
                logger.info("docker tag %s -> %s", safe_pull_ref, safe_image)


def host_project_file_mounts(project_root: Path | str) -> list[tuple[Path, str]]:
    """Host project files bind-mounted at ``/app`` inside the container.

    Images do not COPY ``config.toml`` (it is host-specific). Nested docker
    dispatch looks up ``docker_image`` from that file via ResourceConfig.
    """
    root = Path(project_root)
    mounts: list[tuple[Path, str]] = []
    for name in ("simstack.toml", "config.toml"):
        path = root / name
        if path.is_file():
            mounts.append((path, f"/app/{name}"))
        else:
            logger.warning("Not mounting %s into container; file missing: %s", name, path)
    return mounts


def ensure_host_task_workdir(workdir: Path | str, node_name: str, node_id: str) -> Path:
    """Create the task directory as the host user before Docker/Apptainer runs.

    Containers default to root. If they mkdir ``{workdir}/{node_name}/{id}`` on a
    bind-mounted volume, the node-type directory becomes root-owned mode 755 and
    the runner user can no longer create later task dirs.
    """
    path = Path(workdir) / node_name / str(node_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reload_resource_config() -> None:
    """Re-read config.toml so docker_image assignments from git pull are visible."""
    resource_config = context.resource_config
    if resource_config is None:
        return
    resource_config.reload()
    logger.info("Reloaded resource config from %s", resource_config._config_path)


def _docker_program_config(registry_entry: NodeRegistry, resource_config=None) -> tuple[dict, str]:
    """Look up ``[resource.program.<node>]`` after config.toml has been reloaded.

    ``self`` means the resource on which this runner is currently executing.
    """
    resource_config = resource_config if resource_config is not None else context.resource_config
    task_resource = str(registry_entry.parameters.resource)
    lookup_resource = (
        str(context.config.resource) if task_resource == "self" else task_resource
    )
    try:
        program_config = resource_config.get_program(
            registry_entry.name, resource=lookup_resource
        )
    except ValueError:
        program_config = {}
    return program_config, lookup_resource


async def run_docker_with_outcome(registry_entry: NodeRegistry, *, on_observed_process=None,
                                retry_unpersisted_evidence=False) -> DockerRunResult:
    from simstack.runner_runtime.execution_environment import task_project_root, task_resource_config, task_environment
    from simstack.runner_runtime.launch_claims import task_execution, current_execution
    launch_entry = registry_entry
    local_owner = getattr(launch_entry, "_execution_owner", None)
    if getattr(launch_entry, "_execution_local", False) and (
            not local_owner or local_owner["process_id"] != os.getpid()):
        raise ValueError("A local container must be launched by its admitted physical owner")
    with task_execution(launch_entry):
        execution = current_execution()
        if execution is None:
            raise RuntimeError("A container launch requires its claim identity")
        object.__setattr__(launch_entry, "_execution_start_id", execution["start_id"])
    _reload_resource_config()
    resource_config = task_resource_config(registry_entry, context)
    program_config, lookup_resource = _docker_program_config(registry_entry, resource_config)

    image = program_config.get("docker_image", None)
    if image is None:
        logger.error(f"Docker image for {registry_entry.name} not found for task_id: {registry_entry.id} (looked up resource={lookup_resource})")
        error = f"Docker image for {registry_entry.name} not found (resource={lookup_resource})"
        await _persist_post_launch_failure(launch_entry, error)
        if local_owner:
            from simstack.core.run_node import _confirm_container_stopped
            await _confirm_container_stopped(launch_entry, error, in_docker=False, server=context.server)
        return DockerRunResult(False, "none", error)
    admitted_container = getattr(launch_entry, "_execution_container_owner", None) == execution
    docker_cmd = "docker" if admitted_container else program_config.get("docker_cmd", None)
    if docker_cmd is None:
        docker_cmd = "docker"
    if docker_cmd in {"docker", "apptainer"}:
        object.__setattr__(launch_entry, "_execution_container_backend", docker_cmd)
        if local_owner:
            # Publish selection before science can start. The host controller,
            # rather than the container CLI, remains the admitted physical owner.
            from simstack.server_api.http_transport import RunnerAPIError
            while True:
                try:
                    await context.server.tasks.record_process(launch_entry,
                        local_owner["process_id"], host_owner=True, container_backend=docker_cmd)
                    break
                except RunnerAPIError as exc:
                    if exc.status_code is not None and exc.status_code < 500:
                        raise
                    logger.warning("Local execution owner awaits container backend registration", exc_info=True)
                    await asyncio.sleep(1)

    if docker_cmd == "apptainer":
        if not image.endswith(".sif") and not image.startswith("docker://"):
             image += ".sif"

    workdir = context.config.workdir
    task_dir = ensure_host_task_workdir(workdir, registry_entry.name, str(registry_entry.id))
    cidfile: Path | None = None
    if docker_cmd == "docker":
        cidfile = docker_cidfile_path(task_dir)
    project_file_mounts = host_project_file_mounts(task_project_root(registry_entry, context.config.project_root))
    container_project_root = "/app"
    if context.server.requests.bootstrap_data is None:
        await context.server.resource.bootstrap()
    container_environment = task_environment(registry_entry, server=context.server)
    for name in ("SIMSTACK_NATIVE_OBSERVATION_ID", "SIMSTACK_NATIVE_OBSERVATION_EXECUTION"):
        container_environment.pop(name, None)
        container_environment.pop("APPTAINERENV_" + name, None)
    container_environment["SIMSTACK_CONTAINER_BACKEND"] = docker_cmd
    container_environment["SIMSTACK_PENDING_FAILURE_DIR"] = f"{CONTAINER_WORKDIR}/.pending-execution-failures"
    slurm_container = not admitted_container and registry_entry.parameters.queue == "slurm-queue"
    managed_container = docker_cmd == "docker" and not local_owner and not slurm_container
    if slurm_container:
        container_environment["SIMSTACK_SLURM_CONTAINER_EXECUTION"] = "1"
    container_auth_args = []
    if container_environment.get("SIMSTACK_MACHINE_CONFIG"):
        machine_config = Path(container_environment["SIMSTACK_MACHINE_CONFIG"])
        project_file_mounts.append((machine_config, "/simstack-machine-config.toml:ro"))
        container_environment["SIMSTACK_MACHINE_CONFIG"] = "/simstack-machine-config.toml"
    if getattr(registry_entry, "repository_revision", None):
        from pathlib import Path as RuntimePath
        core_root = RuntimePath(__file__).resolve().parents[1]
        project_file_mounts.extend([(task_project_root(registry_entry, context.config.project_root), "/simstack-source:ro"), (core_root, "/simstack-core/simstack:ro")])
        container_project_root = "/simstack-source"
        container_environment["SIMSTACK_REPOSITORY_ROOT"] = container_project_root
        container_environment["PYTHONPATH"] = "/simstack-core:/simstack-source/src:/simstack-source"
    if container_environment.get("SIMSTACK_RUNNER_CREDENTIALS"):
        if docker_cmd == "docker":
            from simstack.runner_runtime.runner_credentials import CONTAINER_CREDENTIAL_PATH, CONTAINER_CREDENTIAL_TMPFS
            container_environment["SIMSTACK_RUNNER_CREDENTIALS"] = CONTAINER_CREDENTIAL_PATH
            container_auth_args = ["--tmpfs", CONTAINER_CREDENTIAL_TMPFS]
        else:
            credentials_path = Path(container_environment["SIMSTACK_RUNNER_CREDENTIALS"])
            project_file_mounts.append((credentials_path, "/simstack-runner-credentials.json:ro"))
            container_environment["SIMSTACK_RUNNER_CREDENTIALS"] = "/simstack-runner-credentials.json"
    slurm_parameters = getattr(registry_entry.parameters, "slurm_parameters", None)
    resource_args = container_resource_args(docker_cmd, slurm_parameters)
    if resource_args:
        logger.info(
            "task_id: %s applying container resource limits from slurm_parameters: %s",
            registry_entry.id,
            shlex.join(resource_args),
        )
    elif docker_cmd == "apptainer" and (
        docker_cpu_limit(slurm_parameters) is not None
        or docker_memory_limit(slurm_parameters) is not None
    ):
        logger.info(
            "task_id: %s omitting apptainer --cpus/--memory (need cgroups v2); "
            "Slurm already limits the job",
            registry_entry.id,
        )

    # Prefer the task resource inside the container (self -> local for image/workdir lookups).
    container_resource = lookup_resource
    container_name = None
    container_daemon = None
    replay_container = False
    if docker_cmd == "docker":
        docker_registry = None
        if resource_config is not None:
            docker_registry = resource_config.get_docker_registry(lookup_resource)
        await pull_docker_image(image, docker_registry)
        if cidfile is None:
            raise RuntimeError("docker cidfile was not prepared")
        container_name = docker_execution_name(context.server, execution)
        from simstack.runner_runtime.launch_claims import record_container_launch
        if managed_container:
            container_daemon = await asyncio.to_thread(docker_daemon_identity)
        replay_container = record_container_launch(context.server, launch_entry, container_name,
            **({"daemon_id": container_daemon} if managed_container else {}))
        if managed_container:
            from simstack.server_api.http_transport import RunnerAPIError
            while True:
                try:
                    await context.server.tasks.admit_container(launch_entry)
                    break
                except RunnerAPIError as exc:
                    if exc.status_code is not None and exc.status_code < 500:
                        raise
                    await asyncio.sleep(1)
        if replay_container:
            # Its previous CLI may have committed a daemon-owned container.
            object.__setattr__(launch_entry, "_science_cleanup_pending", True)
        project_mount_args: list[str] = []
        for host_path, dest in project_file_mounts:
            project_mount_args.extend(["-v", f"{host_path}:{dest}"])
        cmd = [
            "docker", "run",
            "--name", container_name,
            "--label", "simstack.execution=" + container_name,
            "--cidfile", str(cidfile),
            *container_auth_args,
            *resource_args,
            *(
                [item for key in ("SIMSTACK_SERVER_URL", "SIMSTACK_USER_TOKEN", "SIMSTACK_LOCAL_EXECUTION_TOKEN", "SIMSTACK_RUNNER_TOKEN", "SIMSTACK_RUNNER_CREDENTIALS", "SIMSTACK_REPOSITORY_REVISION", "SIMSTACK_REPOSITORY_ROOT", "SIMSTACK_MACHINE_CONFIG", "SIMSTACK_EXECUTION_TASK", "SIMSTACK_EXECUTION_GENERATION", "SIMSTACK_EXECUTION_CLAIM", "SIMSTACK_EXECUTION_START", "SIMSTACK_EXECUTION_RESOURCE", "SIMSTACK_EXECUTION_ACCOUNT_ID", "SIMSTACK_PENDING_FAILURE_DIR", "SIMSTACK_SLURM_CONTAINER_EXECUTION", "SIMSTACK_CONTAINER_BACKEND", "PYTHONPATH") if key in container_environment for item in ("-e", key)]
            ),
            "-v", f"{workdir}:{CONTAINER_WORKDIR}",
            "-v", f"{workdir}:{LEGACY_CONTAINER_WORKDIR}",
            *project_mount_args,
            image,
            "--node-id", str(registry_entry.id),
            "--resource", container_resource,
            "--project-root", container_project_root,
            "--in-docker",
        ]
    elif docker_cmd == "apptainer":
        bind_args = [
            "--no-mount", "tmp",
            "--bind", f"{workdir}:{CONTAINER_WORKDIR}",
            "--bind", f"{workdir}:{LEGACY_CONTAINER_WORKDIR}",
        ]
        scratch = os.environ.get("SCRATCH", "").strip()
        if scratch:
            scratch_path = Path(scratch)
            scratch_root = scratch_path.resolve()
            mount_roots = {
                Path(root).resolve() for root in ("/tmp", "/scratch", "/ramdisk")
            }
            if scratch_root not in mount_roots and scratch_path.is_dir():
                bind_args.extend(["--bind", f"{scratch_path}:{scratch_path}"])
        for host_path, dest in project_file_mounts:
            bind_args.extend(["--bind", f"{host_path}:{dest}"])
        cmd = [
            "apptainer", "run",
            *resource_args,
            *bind_args,
            image,
            "--node-id", str(registry_entry.id),
            "--resource", container_resource,
            "--project-root", container_project_root,
            "--in-docker",
        ]
    else:
        error = f"Unsupported command {docker_cmd}"
        await _persist_post_launch_failure(launch_entry, error)
        if local_owner:
            from simstack.core.run_node import _confirm_container_stopped
            await _confirm_container_stopped(launch_entry, error, in_docker=False, server=context.server)
        return DockerRunResult(False, "none", error)

    if docker_cmd == "apptainer":
        for key in ("SIMSTACK_SERVER_URL", "SIMSTACK_USER_TOKEN", "SIMSTACK_LOCAL_EXECUTION_TOKEN", "SIMSTACK_RUNNER_TOKEN", "SIMSTACK_RUNNER_CREDENTIALS", "SIMSTACK_REPOSITORY_REVISION", "SIMSTACK_REPOSITORY_ROOT", "SIMSTACK_MACHINE_CONFIG", "SIMSTACK_EXECUTION_TASK", "SIMSTACK_EXECUTION_GENERATION", "SIMSTACK_EXECUTION_CLAIM", "SIMSTACK_EXECUTION_START", "SIMSTACK_EXECUTION_RESOURCE", "SIMSTACK_EXECUTION_ACCOUNT_ID", "SIMSTACK_PENDING_FAILURE_DIR", "SIMSTACK_SLURM_CONTAINER_EXECUTION", "SIMSTACK_CONTAINER_BACKEND", "PYTHONPATH"):
            if key in container_environment:
                container_environment["APPTAINERENV_" + key] = container_environment[key]

    sanitized_cmd = sanitized_command(cmd, None)

    logger.info(
        "starting docker for task_id: %s full docker command: %s",
        registry_entry.id,
        shlex.join(sanitized_cmd),
    )

    # DETACHED_PROCESS opens a console on Windows. CREATE_NO_WINDOW hides it.
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000
        )

    process_started = False
    canonical = None
    child_outcome = None
    exit_code = None
    failure_cause = None
    pending_failure = None
    observation = None
    observation_lease = None

    async def observe_container():
        while True:
            try:
                if managed_container and await asyncio.to_thread(docker_daemon_identity) != container_daemon:
                    raise RuntimeError("Docker execution belongs to a different daemon")
                return await asyncio.to_thread(inspect_docker_execution, container_name)
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                logger.warning("Task %s awaits Docker execution evidence", launch_entry.id, exc_info=True)
                await asyncio.sleep(1)

    try:
        from simstack.runner_runtime.launch_claims import inherit_launch_reservation
        if (docker_cmd == "apptainer" and not local_owner
                and registry_entry.parameters.queue == "default" and os.name != "nt"):
            from simstack.runner_runtime import native_observations
            observation, observation_lease = native_observations.prepare(context.server, launch_entry)
        while True:
            actual_command = cmd
            if docker_cmd == "docker":
                observed = await observe_container() if replay_container else None
                if observed is None:
                    prepare_docker_cidfile(task_dir)
                else:
                    action = ["start", "--attach"] if observed["State"]["Status"] == "created" else ["logs", "--follow"]
                    actual_command = ["docker", *action, container_name]
            with inherit_launch_reservation(getattr(launch_entry, "_launch_reservation", None), container_environment) as ownership:
                try:
                    object.__setattr__(launch_entry, "_science_cleanup_pending", True)
                    from contextlib import nullcontext
                    observation_owner = (native_observations.inherit(observation_lease, ownership)
                                         if observation is not None else nullcontext(ownership))
                    with observation_owner as ownership:
                        if observation is not None:
                            actual_command = native_observations.observed_command(context.server, observation, actual_command)
                        process = await asyncio.create_subprocess_exec(
                            *actual_command,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            creationflags=creationflags,
                            start_new_session=platform.system() != "Windows",
                            env=container_environment,
                            **ownership,
                        )
                    process_started = True
                    if observation is not None:
                        process._simstack_process_group = True
                        native_observations.spawned(context.server, observation, process.pid)
                        if on_observed_process is not None:
                            on_observed_process(process, observation)
                except OSError:
                    if docker_cmd != "docker" or not replay_container:
                        object.__setattr__(launch_entry, "_science_cleanup_pending", False)
                        raise
                    logger.warning("Task %s awaits attachment to its Docker execution", launch_entry.id, exc_info=True)
                    await asyncio.sleep(1)
                    continue
                finally:
                    if observation_lease is not None:
                        observation_lease.close()
                        observation_lease = None
            process_started = True
            if docker_cmd == "apptainer" and os.name != "nt" and isinstance(process, asyncio.subprocess.Process):
                process._simstack_process_group = True
            from simstack.runner_runtime.launch_claims import record_spawn
            record_spawn(context.server, launch_entry, os.getpid() if local_owner else process.pid)
            from simstack.core.services.task_termination import record_process_id
            try:
                if not local_owner:
                    await record_process_id(launch_entry, process.pid, context.server)
            except Exception:
                logger.warning("Task %s process report awaits reconciliation", launch_entry.id, exc_info=True)
            if local_owner:
                from simstack.core.services.task_termination import monitor_task_process
                stdout_b, stderr_b = await monitor_task_process(launch_entry, process, server=context.server,
                    workdir=workdir, runner_pid=os.getpid())
            else:
                stdout_b, stderr_b = await process.communicate()
            exit_code = process.returncode
            child_outcome = parse_run_node_result(sanitized_tail(stdout_b, None))
            if docker_cmd == "docker" and (replay_container or exit_code != 0):
                observed = await observe_container()
                if observed is not None:
                    if observed["State"]["Status"] not in {"exited", "dead"}:
                        replay_container = True
                        continue
                    exit_code = observed["State"]["ExitCode"]
                elif exit_code < 0:
                    # A killed CLI cannot tell whether its create committed.
                    # Repeating the same Docker name cannot create a second job.
                    replay_container = True
                    continue
            if exit_code != 0:
                failure_cause = format_container_failure_error(exit_code, docker_cmd=docker_cmd)
                from simstack.runner_runtime.pending_execution_failures import record_observed_failure
                pending_failure = record_observed_failure(
                    (child_outcome.error if child_outcome is not None else None) or failure_cause, phase="container",
                    execution=context.server.tasks.execution(launch_entry), server=context.server,
                    resource=getattr(launch_entry, "_execution_resource", None),
                    exit_code=exit_code if exit_code >= 0 else None,
                    signal=-exit_code if exit_code < 0 else None,
                    return_kind=child_outcome.return_kind if child_outcome is not None else "exception",
                    # The normal Runner poll can replay this receipt while the
                    # observer still awaits its owned Apptainer descendants.
                    cleanup={"scope": observation["scope"], "process_group": process.pid}
                        if observation is not None else None)
            if observation is not None:
                from simstack.core.services.task_termination import stop_owned_process_group, TaskTerminationError
                while True:
                    try:
                        await stop_owned_process_group(process)
                        break
                    except TaskTerminationError:
                        logger.warning("Task %s awaits its owned Apptainer process group", launch_entry.id, exc_info=True)
                        await asyncio.sleep(1)
            canonical = await context.server.tasks.get_outcome(launch_entry)
            if local_owner and canonical is not None and canonical.status == TaskStatus.TERMINATING:
                from simstack.core.services.task_termination import stop_task_execution, TaskTerminationError
                from simstack.server_api.http_transport import RunnerAPIError
                while True:
                    try:
                        await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                            workdir=workdir, local_process=process)
                        canonical = await context.server.tasks.termination_confirmed(launch_entry, canonical.error or "Terminated by user")
                        break
                    except (RunnerAPIError, TaskTerminationError):
                        logger.warning("Task %s retains its container until stop is acknowledged", launch_entry.id, exc_info=True)
                        await asyncio.sleep(1)
            break
        stdout = sanitized_tail(stdout_b, None)
        stderr = sanitized_tail(stderr_b, None)
        if canonical is None:
            raise RuntimeError("Container task outcome is unavailable")
        if canonical.status == TaskStatus.COMPLETED:
            # A launcher/cleanup failure cannot revoke a committed result.
            outcome = DockerRunResult(True, canonical.return_kind or (
                child_outcome.return_kind if child_outcome else "none"))
        elif exit_code != 0 or child_outcome is None or not child_outcome.success:
            base_error = format_container_failure_error(exit_code,
                oom_killed=_container_oom_killed(docker_cmd, cidfile),
                memory_limit=docker_memory_limit(slurm_parameters) if docker_cmd == "docker" else None,
                docker_cmd=docker_cmd)
            error = canonical.error or (child_outcome.error if child_outcome else None) or base_error
            error = sanitized_tail(error, None, limit=None)
            visible_stdout = "\n".join(line for line in stdout.splitlines() if not line.startswith(RESULT_PREFIX))
            diagnostics = [base_error, *(part for part in (visible_stdout, stderr) if part)]
            logger.error("Container task_id: %s failed: %s", launch_entry.id, error)
            await _persist_post_launch_failure(launch_entry, error, diagnostics=diagnostics, returncode=exit_code,
                return_kind=canonical.return_kind or (child_outcome.return_kind if child_outcome else "exception"),
                pending_failure=pending_failure)
            outcome = DockerRunResult(False, child_outcome.return_kind if child_outcome else "exception", error)
        else:
            error = canonical.error or "Container exited without completing its task"
            await _persist_post_launch_failure(launch_entry, error, returncode=exit_code, return_kind=child_outcome.return_kind)
            outcome = DockerRunResult(False, child_outcome.return_kind, error)
    except Exception as exc:
        if process_started and (local_owner or docker_cmd == "docker" or process.returncode is None):
            from simstack.core.services.task_termination import stop_task_execution, TaskTerminationError
            from simstack.server_api.http_transport import RunnerAPIError
            while True:
                try:
                    await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                        workdir=workdir, local_process=process)
                    await process.wait()
                    break
                except (RunnerAPIError, TaskTerminationError):
                    if not local_owner and docker_cmd != "docker":
                        raise
                    logger.warning("Task %s retains its container until cleanup is verified", launch_entry.id, exc_info=True)
                    await asyncio.sleep(1)
            if local_owner and not (child_outcome is not None and child_outcome.success):
                while True:
                    try:
                        canonical = await context.server.tasks.get_outcome(launch_entry)
                        if canonical is not None and canonical.status == TaskStatus.TERMINATING:
                            canonical = await context.server.tasks.termination_confirmed(
                                launch_entry, canonical.error or "Terminated by user")
                        break
                    except RunnerAPIError as failure:
                        if failure.status_code is not None and failure.status_code < 500:
                            break
                        logger.warning("Task %s awaits its stopped-container outcome", launch_entry.id, exc_info=True)
                        await asyncio.sleep(1)
        error = sanitized_tail(str(exc), None)
        logger.error("Container launch for task %s failed: %s", launch_entry.id, error)
        if canonical is not None and canonical.status == TaskStatus.COMPLETED:
            outcome = DockerRunResult(True, canonical.return_kind or "none")
        elif child_outcome is not None and child_outcome.success:
            # The child only reports success after Server accepts completion.
            # A subsequent read outage is supplemental launcher diagnostics.
            outcome = DockerRunResult(True, child_outcome.return_kind)
        else:
            primary = (canonical.error if canonical is not None and canonical.error else
                (child_outcome.error if child_outcome is not None else None) or failure_cause or error)
            await _persist_post_launch_failure(launch_entry, primary, returncode=exit_code,
                return_kind=(canonical.return_kind if canonical is not None else None)
                    or (child_outcome.return_kind if child_outcome else "exception"),
                pending_failure=pending_failure)
            outcome = DockerRunResult(False, "exception", primary)
    finally:
        if observation_lease is not None:
            observation_lease.close()

    if pending_failure is not None:
        from simstack.runner_runtime.pending_execution_failures import reconcile_observed_failure
        while not await reconcile_observed_failure(context.server, pending_failure):
            if pending_failure[0] is not None or not retry_unpersisted_evidence:
                break
            await asyncio.sleep(1)
    if observation is not None:
        # Recovery validates the inherited lease and the whole host process
        # group; a CLI exit alone never proves all scientific descendants dead.
        await native_observations.recover(context.server)
    if (slurm_container or managed_container) and docker_cmd == "docker":
        # The compute-side Docker owner can also acknowledge bootstrap/OOM
        # failures where an inside-container supervisor never got to start.
        observed = await observe_container() if process_started else None
        if observed is None or observed["State"]["Status"] in {"exited", "dead"}:
            from simstack.server_api.http_transport import RunnerAPIError
            while True:
                try:
                    await context.server.tasks.termination_confirmed(launch_entry,
                        outcome.error or "Scientific execution stopped")
                    if managed_container:
                        from simstack.runner_runtime.launch_claims import receipt, finish
                        value = receipt(context.server, launch_entry.id)
                        if value is not None and value.get("claim_id") == execution["claim_id"]:
                            finish(context.server, value, claimed=False)
                    break
                except RunnerAPIError as exc:
                    if managed_container and exc.status_code == 409:
                        # A durable failure may not have reached Server yet.
                        # The retained daemon receipt retries after failure
                        # reconciliation, without falsely acknowledging stop.
                        break
                    if exc.status_code is not None and exc.status_code < 500:
                        raise
                    await asyncio.sleep(1)
    if local_owner:
        if process_started:
            from simstack.core.services.task_termination import stop_task_execution, TaskTerminationError
            from simstack.server_api.http_transport import RunnerAPIError
            while True:
                try:
                    await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                        workdir=workdir, local_process=process)
                    object.__setattr__(launch_entry, "_science_cleanup_pending", False)
                    break
                except (RunnerAPIError, TaskTerminationError, OSError):
                    logger.warning("Local container owner awaits verified physical cleanup", exc_info=True)
                    await asyncio.sleep(1)
        # Only the host owner can acknowledge its entire drained execution.
        from simstack.core.run_node import _confirm_container_stopped
        await _confirm_container_stopped(launch_entry,
            outcome.error or "Scientific execution stopped", in_docker=False, server=context.server)
    if process_started and docker_cmd == "docker" and platform.system() != "Windows":
        try:
            owner_uid, owner_gid = launcher_owner_ids()
            await _harmonize_docker_task_dir(image, task_dir, owner_uid, owner_gid)
        except Exception as exc:
            logger.error("Task %s output ownership cleanup failed: %s", launch_entry.id,
                sanitized_tail(str(exc), None))
    return outcome


async def run_docker(registry_entry: NodeRegistry, *, on_observed_process=None, retry_unpersisted_evidence=False) -> bool:
    """Run a container while preserving the existing boolean API."""
    return (await run_docker_with_outcome(registry_entry, on_observed_process=on_observed_process,
        retry_unpersisted_evidence=retry_unpersisted_evidence)).success
