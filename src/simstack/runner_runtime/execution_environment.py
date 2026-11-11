from pathlib import Path
import os
import sys

from simstack.core.context import context


async def prepare_task(entry):
    if not getattr(entry, "repository_revision", None):
        return None
    from simstack.util.repository_runtime import prepare_task_runtime

    root = await prepare_task_runtime(entry)
    if root is not None:
        object.__setattr__(entry, "_repository_project_root", Path(root))
    return root


def task_project_root(entry, fallback=None):
    root = getattr(entry, "_repository_project_root", None)
    return Path(
        root
        if root is not None
        else (fallback if fallback is not None else context.config.project_root)
    )


def task_launch_command(entry, resource, project_root, *, in_docker=False, local_user=False):
    """Use the prepared revision's interpreter, or the installed Core runtime."""
    from simstack.util.repository_runtime import runtime_python

    root = Path(project_root)
    interpreter = (
        runtime_python(root)
        if entry.repository_revision and not in_docker and not local_user
        else Path(sys.executable)
    )
    command = [
        str(interpreter),
        "-m",
        "simstack.core.run_node",
        "--node-id",
        str(entry.id),
        "--resource",
        str(resource),
        "--project-root",
        str(root),
    ]
    if in_docker:
        command.append("--in-docker")
    return command


def task_resource_config(entry, runtime_context=None):
    from simstack.util.resource_config import ResourceConfig

    active = runtime_context or context
    explicit = os.environ.get("SIMSTACK_MACHINE_CONFIG")
    if explicit:
        return ResourceConfig(Path(explicit), str(active.config.resource))
    root = task_project_root(entry, active.config.project_root)
    if root == active.config.project_root:
        return active.resource_config
    return ResourceConfig(root / "config.toml", str(active.config.resource))


def task_environment(entry, *, server):
    from simstack.server_api.http_transport import runner_environment
    from simstack.runner_runtime.launch_claims import _actor

    result = runner_environment()
    result["SIMSTACK_EXECUTION_ACCOUNT_ID"] = _actor(server)["account_id"]
    if getattr(entry, "_execution_local", False):
        if server.requests.authorization != "local":
            raise ValueError("A local user execution requires its task-scoped API client")
        for name in ("SIMSTACK_RUNNER_TOKEN", "SIMSTACK_SERVER_TOKEN", "SIMSTACK_RUNNER_CREDENTIALS", "SIMSTACK_USER_TOKEN"):
            result.pop(name, None)
        result["SIMSTACK_SERVER_URL"] = server.requests.server_url
        result["SIMSTACK_LOCAL_EXECUTION_TOKEN"] = server.requests.token
    else:
        result.pop("SIMSTACK_USER_TOKEN", None)
        result.pop("SIMSTACK_LOCAL_EXECUTION_TOKEN", None)
    revision = getattr(entry, "repository_revision", None)
    if revision:
        result["SIMSTACK_REPOSITORY_REVISION"] = str(revision)
    from simstack.runner_runtime.launch_claims import task_execution, current_execution

    with task_execution(entry):
        execution = current_execution()
        if execution:
            result["SIMSTACK_EXECUTION_TASK"] = execution["task_id"]
            result["SIMSTACK_EXECUTION_CLAIM"] = execution["claim_id"]
            result["SIMSTACK_EXECUTION_START"] = execution["start_id"]
            resource = getattr(entry, "_execution_resource", None)
            if resource is not None:
                result["SIMSTACK_EXECUTION_RESOURCE"] = str(resource)
            else:
                result.pop("SIMSTACK_EXECUTION_RESOURCE", None)
            generation = getattr(entry, "_execution_generation", None)
            if generation is not None:
                result["SIMSTACK_EXECUTION_GENERATION"] = str(generation)
            else:
                result.pop("SIMSTACK_EXECUTION_GENERATION", None)
        else:
            for key in (
                "SIMSTACK_EXECUTION_TASK",
                "SIMSTACK_EXECUTION_CLAIM",
                "SIMSTACK_EXECUTION_START",
                "SIMSTACK_EXECUTION_GENERATION",
                "SIMSTACK_EXECUTION_RESOURCE",
            ):
                result.pop(key, None)
    return result
