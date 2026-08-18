"""Runtime handoff for detached repository-backed tasks."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from simstack.core.context import context
from simstack.core.definitions import DBType

TASK_ENV_PREFIX = "SIMSTACK_TASK_"


def repository_task_initialization(
    *,
    resource: str,
    project_root: str | None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build context arguments from the explicit child-task handoff protocol."""

    task_environment = os.environ if environment is None else environment
    initialization: dict[str, Any] = {
        "resource": resource,
        "project_root": project_root,
    }
    database_name = task_environment.get("SIMSTACK_TASK_DB_NAME")
    connection_string = task_environment.get("SIMSTACK_TASK_DB_CONNECTION_STRING")
    if not database_name and not connection_string:
        return initialization
    if not database_name or not connection_string:
        raise RuntimeError(
            "Detached task database handoff requires both "
            "SIMSTACK_TASK_DB_NAME and SIMSTACK_TASK_DB_CONNECTION_STRING"
        )

    python_paths = json.loads(
        task_environment.get("SIMSTACK_TASK_PYTHON_PATHS", "[]")
    )
    if not isinstance(python_paths, list):
        raise ValueError("SIMSTACK_TASK_PYTHON_PATHS must contain a JSON list")
    initialization.update(
        {
            "db_name": database_name,
            "connection_string": connection_string,
            "db_type": DBType.MONGODB,
            "python_paths": python_paths,
            "workdir": task_environment.get("SIMSTACK_TASK_WORKDIR", "simstack"),
            "environment_start": task_environment.get(
                "SIMSTACK_TASK_ENVIRONMENT_START", ""
            ),
        }
    )
    return initialization


def repository_task_environment(repository_checkout: Path) -> dict[str, str]:
    """Serialize the current runner context for one detached task process."""

    connection_string = context.db.connection_string
    if not connection_string:
        raise RuntimeError(
            "Repository-backed tasks require a direct database connection"
        )
    task_environment = {
        "SIMSTACK_TASK_DB_NAME": context.db.database_name,
        "SIMSTACK_TASK_DB_CONNECTION_STRING": connection_string,
        "SIMSTACK_TASK_WORKDIR": str(context.config.workdir),
        "SIMSTACK_TASK_ENVIRONMENT_START": context.config.environment_start or "",
        "SIMSTACK_TASK_PYTHON_PATHS": json.dumps([str(repository_checkout)]),
    }
    server_url = os.environ.get("SIMSTACK_SERVER_URL") or getattr(
        context.config, "server_url", None
    )
    if server_url:
        task_environment["SIMSTACK_SERVER_URL"] = str(server_url)
    server_token = os.environ.get("SIMSTACK_RUNNER_TOKEN") or getattr(
        context.config, "server_token", None
    )
    if server_token:
        task_environment["SIMSTACK_RUNNER_TOKEN"] = str(server_token)
    return task_environment
