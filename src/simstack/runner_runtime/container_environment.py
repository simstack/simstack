"""Prepare credentials, repository mounts and environment for a task container."""

import logging
from dataclasses import dataclass
from pathlib import Path

from simstack.core.context import GlobalState
from simstack.models import NodeRegistry
from simstack.runner_runtime import execution_environment

logger = logging.getLogger("DockerRunner")

# In-container alias for the host resource workdir (not scratch). Must already
# exist in typical images. Nested /tmp/simstack is hidden by Apptainer's default
# host-/tmp bind; do not overlay container /tmp.
CONTAINER_WORKDIR = "/mnt"
# Images built before CONTAINER_WORKDIR moved still initialize --in-docker at
# /tmp/simstack. Bind the host workdir there too; --no-mount tmp keeps the
# image mkdir visible.
LEGACY_CONTAINER_WORKDIR = "/tmp/simstack"


FORWARDED_VARIABLES = (
    "SIMSTACK_SERVER_URL",
    "SIMSTACK_USER_TOKEN",
    "SIMSTACK_LOCAL_EXECUTION_TOKEN",
    "SIMSTACK_RUNNER_TOKEN",
    "SIMSTACK_RUNNER_CREDENTIALS",
    "SIMSTACK_REPOSITORY_REVISION",
    "SIMSTACK_REPOSITORY_ROOT",
    "SIMSTACK_MACHINE_CONFIG",
    "SIMSTACK_EXECUTION_TASK",
    "SIMSTACK_EXECUTION_GENERATION",
    "SIMSTACK_EXECUTION_CLAIM",
    "SIMSTACK_EXECUTION_START",
    "SIMSTACK_EXECUTION_RESOURCE",
    "SIMSTACK_EXECUTION_ACCOUNT_ID",
    "SIMSTACK_PENDING_FAILURE_DIR",
    "SIMSTACK_SLURM_CONTAINER_EXECUTION",
    "SIMSTACK_CONTAINER_BACKEND",
    "PYTHONPATH",
)


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


@dataclass
class ContainerEnvironment:
    variables: dict[str, str]
    forward_keys: tuple[str, ...]
    mounts: list[tuple[Path, str]]
    project_root: str
    docker_auth_args: list[str]


async def prepare_container_environment(
    registry_entry: NodeRegistry, context: GlobalState, backend: str, *,
    project_file_mounts: list[tuple[Path, str]], slurm_container: bool,
) -> ContainerEnvironment:
    container_project_root = "/app"
    if context.server.requests.bootstrap_data is None:
        await context.server.resource.bootstrap()
    container_environment = execution_environment.task_environment(registry_entry, server=context.server)
    slurm_keys = ("SLURM_JOB_ID", "SLURM_RESTART_COUNT", "SLURM_ARRAY_JOB_ID", "SLURM_ARRAY_TASK_ID",
                  "SLURM_ARRAY_TASK_COUNT", "SLURM_ARRAY_TASK_MIN", "SLURM_ARRAY_TASK_MAX",
                  "SLURM_ARRAY_TASK_STEP") if "SLURM_ARRAY_TASK_ID" in container_environment else ()

    for name in ("SIMSTACK_NATIVE_OBSERVATION_ID", "SIMSTACK_NATIVE_OBSERVATION_EXECUTION"):
        container_environment.pop(name, None)
        container_environment.pop("APPTAINERENV_" + name, None)
    container_environment["SIMSTACK_CONTAINER_BACKEND"] = backend
    container_environment["SIMSTACK_PENDING_FAILURE_DIR"] = f"{CONTAINER_WORKDIR}/.pending-execution-failures"
    if slurm_container:
        container_environment["SIMSTACK_SLURM_CONTAINER_EXECUTION"] = "1"
        from simstack.util.resource_config import slurm_task_scratch
        checkpoint = slurm_task_scratch(registry_entry.id)
        if checkpoint:
            # Share the job's selected scratch directory with the host signal handler.
            project_file_mounts.append((checkpoint, str(checkpoint)))
            slurm_keys += ("SIMSTACK_SLURM_TASK_SCRATCH",)
            if "TMP_BASE_DIR" in container_environment:
                slurm_keys += ("TMP_BASE_DIR",)
    container_auth_args = []
    if container_environment.get("SIMSTACK_MACHINE_CONFIG"):
        machine_config = Path(container_environment["SIMSTACK_MACHINE_CONFIG"])
        project_file_mounts.append((machine_config, "/simstack-machine-config.toml:ro"))
        container_environment["SIMSTACK_MACHINE_CONFIG"] = "/simstack-machine-config.toml"
    if getattr(registry_entry, "repository_revision", None):
        core_root = Path(__file__).resolve().parents[1]
        project_file_mounts.extend([(execution_environment.task_project_root(registry_entry, context.config.project_root), "/simstack-source:ro"), (core_root, "/simstack-core/simstack:ro")])
        container_project_root = "/simstack-source"
        container_environment["SIMSTACK_REPOSITORY_ROOT"] = container_project_root
        container_environment["PYTHONPATH"] = "/simstack-core:/simstack-source/src:/simstack-source"
    if container_environment.get("SIMSTACK_RUNNER_CREDENTIALS"):
        if backend == "docker":
            from simstack.runner_runtime.runner_credentials import (
                CONTAINER_CREDENTIAL_PATH,
                CONTAINER_CREDENTIAL_TMPFS,
            )
            container_environment["SIMSTACK_RUNNER_CREDENTIALS"] = CONTAINER_CREDENTIAL_PATH
            container_auth_args = ["--tmpfs", CONTAINER_CREDENTIAL_TMPFS]
        else:
            credentials_path = Path(container_environment["SIMSTACK_RUNNER_CREDENTIALS"])
            project_file_mounts.append((credentials_path, "/simstack-runner-credentials.json:ro"))
            container_environment["SIMSTACK_RUNNER_CREDENTIALS"] = "/simstack-runner-credentials.json"
    forward_keys = (*FORWARDED_VARIABLES, *slurm_keys)
    if backend == "apptainer":
        for key in forward_keys:
            if key in container_environment:
                container_environment["APPTAINERENV_" + key] = container_environment[key]
    return ContainerEnvironment(
        container_environment, forward_keys,
        project_file_mounts, container_project_root, container_auth_args,
    )
