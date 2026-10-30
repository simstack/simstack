from pathlib import Path

from simstack.core.context import context
from simstack.core.definitions import DBType


def is_api_runner(runtime_context=None):
    try:
        return (runtime_context or context).db.databae_type == DBType.API
    except (RuntimeError, AttributeError):
        return False


async def prepare_task(entry, runtime_context=None):
    if not is_api_runner(runtime_context) or not getattr(
        entry, "repository_revision", None
    ):
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


def task_resource_config(entry, runtime_context=None):
    from simstack.util.resource_config import ResourceConfig

    active = runtime_context or context
    root = task_project_root(entry, active.config.project_root)
    if root == active.config.project_root:
        return active.resource_config
    return ResourceConfig(root / "config.toml", str(active.config.resource))


def task_environment(entry):
    from simstack.runner_api.transport import runner_environment

    result = runner_environment()
    revision = getattr(entry, "repository_revision", None)
    if revision:
        result["SIMSTACK_REPOSITORY_REVISION"] = str(revision)
    return result
