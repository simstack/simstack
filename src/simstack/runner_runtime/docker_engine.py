"""Docker daemon inspection, image preparation and CLI arguments."""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path

from simstack.runner_runtime.container_environment import (
    CONTAINER_WORKDIR,
    LEGACY_CONTAINER_WORKDIR,
    ContainerEnvironment,
)
from simstack.util.output_permissions import docker_harmonize_command
from simstack.util.sanitized_output import sanitized_tail

logger = logging.getLogger("DockerRunner")


_DOCKER_HUB_LIBRARY_PREFIX = "docker.io/library/"
_pull_locks: dict[str, asyncio.Lock] = {}
_DOCKER_CIDFILE_NAME = ".docker_cid"


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


def build_docker_command(
    environment: ContainerEnvironment, *, image: str, workdir: Path | str,
    resource_args: list[str], task_args: list[str], container_name: str, cidfile: Path,
) -> list[str]:
    project_mount_args: list[str] = []
    for host_path, dest in environment.mounts:
        project_mount_args.extend(["-v", f"{host_path}:{dest}"])
    env_args = []
    for key in environment.forward_keys:
        if key in environment.variables:
            env_args.extend(["-e", key])
    return [
        "docker", "run",
        "--name", container_name,
        "--label", "simstack.execution=" + container_name,
        "--cidfile", str(cidfile),
        *environment.docker_auth_args,
        *resource_args,
        *env_args,
        "-v", f"{workdir}:{CONTAINER_WORKDIR}",
        "-v", f"{workdir}:{LEGACY_CONTAINER_WORKDIR}",
        *project_mount_args,
        image,
        *task_args,
    ]
