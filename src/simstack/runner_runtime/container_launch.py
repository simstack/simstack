"""Build explicit container commands without putting API secrets in argv."""

from pathlib import Path
from simstack.models import NodeRegistry
from simstack.runner_api import ExecutionRunnerClient
from simstack.runner_runtime.container_limits import container_resource_args
from simstack.runner_runtime.execution_adapter import ContainerBackend
from simstack.runner_runtime.environment import sanitized_launcher_environment, sanitized_runner_environment, worker_environment_allowlist_value


def container_launch(registry_entry: NodeRegistry, *, claim_id: str, attempt: int,
                     runner_api: ExecutionRunnerClient, config, execution_adapter) -> tuple[list[str], dict[str, str]]:
    resource = str(config.resource)
    profile = getattr(registry_entry, "runtime_profile", None)
    adapter = execution_adapter.container(registry_entry.name, **({"runtime_profile": profile} if profile else {}))
    environment_allowlist = adapter.boundary.environment_allowlist
    container_project_root = str(adapter.project_root)
    container_workdir = str(adapter.workdir)

    task_id = str(registry_entry.id)
    api_environment = {
        "SIMSTACK_SERVER_URL": runner_api.server_url,
        "SIMSTACK_RUNNER_TOKEN": runner_api.runner_token,
        "SIMSTACK_TASK_ID": task_id,
        "SIMSTACK_TASK_CLAIM_ID": claim_id,
        "SIMSTACK_TASK_ATTEMPT": str(attempt),
        "SIMSTACK_RUNNER_RESOURCE": resource,
        "SIMSTACK_PROJECT_ROOT": container_project_root,
        "SIMSTACK_RUNNER_WORKDIR": container_workdir,
        "SIMSTACK_WORKER_ENVIRONMENT_ALLOWLIST": (
            worker_environment_allowlist_value(environment_allowlist)
        ),
    }
    worker_environment = sanitized_runner_environment(
        api_environment,
        allowed_environment_names=environment_allowlist,
        workflow_bootstrap=bool(getattr(registry_entry, "workflow_revision", None)),
    )
    child_environment = sanitized_launcher_environment(
        api_environment,
        allowed_environment_names=environment_allowlist,
        workflow_bootstrap=bool(getattr(registry_entry, "workflow_revision", None)),
    )

    worker_args = [
        "--node-id",
        task_id,
        "--claim-id",
        claim_id,
        "--attempt",
        str(attempt),
        "--resource",
        resource,
        "--project-root",
        container_project_root,
    ]
    host_project_root = Path(config.project_root).resolve()
    host_workdir = Path(config.workdir).resolve()
    from simstack.runner_runtime.workflow_installation import packaged_worker_command

    worker_command = packaged_worker_command(adapter.guarded_worker_command, registry_entry, workdir=host_workdir)
    if getattr(registry_entry, "workflow_revision", None):
        # The launcher already mounts the resource workdir into the container.
        worker_command = tuple(str(adapter.workdir / Path(item).relative_to(host_workdir)) if Path(item).is_absolute() and Path(item).is_relative_to(host_workdir) else item for item in worker_command)
    if adapter.backend is ContainerBackend.DOCKER:
        cmd = [
            *adapter.launcher_command,
            "run",
            "--rm",
            *container_resource_args("docker", registry_entry.parameters.slurm_parameters),
            "-w",
            container_workdir,
        ]
        for name in worker_environment:
            # Docker reads the value from child_environment. Secrets never enter argv.
            cmd.extend(("-e", name))
        cmd.extend(
            (
                "-v",
                f"{host_project_root}:{container_project_root}:ro",
                "-v",
                f"{host_workdir}:{container_workdir}",
                adapter.image,
                *worker_command,
                *worker_args,
            )
        )
    elif adapter.backend is ContainerBackend.APPTAINER:
        for name, value in worker_environment.items():
            child_environment[f"APPTAINERENV_{name}"] = value
        cmd = [
            *adapter.launcher_command,
            "run",
            "--cleanenv",
            *container_resource_args("apptainer", registry_entry.parameters.slurm_parameters),
            "--pwd",
            container_workdir,
            "--bind",
            f"{host_project_root}:{container_project_root}:ro",
            "--bind",
            f"{host_workdir}:{container_workdir}",
            adapter.image,
            *worker_command,
            *worker_args,
        ]


    return cmd, child_environment
