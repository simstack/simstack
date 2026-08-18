from pathlib import Path

import pytest

from simstack.core.definitions import DBType
from simstack.core.repository_task_runtime import repository_task_initialization


def test_repository_task_initialization_uses_explicit_database_handoff():
    initialization = repository_task_initialization(
        resource="runner-a",
        project_root="/cache/repo/commit",
        environment={
            "SIMSTACK_TASK_DB_NAME": "user-a",
            "SIMSTACK_TASK_DB_CONNECTION_STRING": "mongodb://runner:user@mongo",
            "SIMSTACK_TASK_PYTHON_PATHS": '["/cache/repo/commit"]',
            "SIMSTACK_TASK_WORKDIR": "/work",
            "SIMSTACK_TASK_ENVIRONMENT_START": "module load gromacs",
        },
    )

    assert initialization == {
        "resource": "runner-a",
        "project_root": "/cache/repo/commit",
        "db_name": "user-a",
        "connection_string": "mongodb://runner:user@mongo",
        "db_type": DBType.MONGODB,
        "python_paths": ["/cache/repo/commit"],
        "workdir": "/work",
        "environment_start": "module load gromacs",
    }


def test_repository_task_initialization_preserves_configured_runner_path_without_handoff():
    assert repository_task_initialization(
        resource="existing-runner",
        project_root=None,
        environment={},
    ) == {"resource": "existing-runner", "project_root": None}


@pytest.mark.parametrize(
    "environment",
    [
        {"SIMSTACK_TASK_DB_NAME": "user-a"},
        {"SIMSTACK_TASK_DB_CONNECTION_STRING": "mongodb://mongo"},
    ],
)
def test_repository_task_initialization_rejects_partial_database_handoff(environment):
    with pytest.raises(RuntimeError, match="requires both"):
        repository_task_initialization(
            resource="runner-a",
            project_root=str(Path("repo")),
            environment=environment,
        )
