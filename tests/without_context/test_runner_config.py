from pathlib import Path

import pytest

from simstack.runner_runtime.config import RunnerConfig, runner_task_workdir


def test_runner_config_is_built_from_server_bootstrap(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    payload = {
        "resource": {
            "resource_str": "local",
            "workdir": str(workdir),
            "hostname": "runner-host",
            "python_paths": ["$PROJECT/src"],
            "environment_start": None,
            "is_default": True,
        },
        "capabilities": {
            "max_runner_payload_bytes": 8_000_000,
            "max_inline_document_bytes": 4_000_000,
        },
    }

    config = RunnerConfig.from_bootstrap(
        payload,
        project_root=tmp_path,
        server_url="https://simstack.example/api",
    )

    assert str(config.resource) == "local"
    assert config.workdir == workdir
    assert config.python_paths == [Path(tmp_path) / "src"]
    assert config.environment_start is None
    assert config.is_default is True
    assert not hasattr(config, "git_list")
    assert not hasattr(config, "git_branch")
    assert config.max_runner_payload_bytes == 8_000_000
    assert config.max_inline_document_bytes == 4_000_000


def test_runner_config_rejects_incomplete_resource(tmp_path):
    with pytest.raises(ValueError, match="exact v1 DTO"):
        RunnerConfig.from_bootstrap(
            {"resource": {"resource_str": "local"}},
            project_root=tmp_path,
            server_url="https://simstack.example/api",
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("resource_str", 1),
        ("workdir", " /tmp/work "),
        ("hostname", ""),
        ("python_paths", [1]),
        ("environment_start", " "),
        ("is_default", 1),
    ),
)
def test_runner_config_rejects_coerced_resource_fields(tmp_path, field, invalid_value):
    resource = {
        "resource_str": "local",
        "workdir": "/tmp/work",
        "hostname": "runner-host",
        "python_paths": [],
        "environment_start": None,
        "is_default": False,
    }
    resource[field] = invalid_value

    with pytest.raises(ValueError):
        RunnerConfig.from_bootstrap(
            {
                "resource": resource,
                "capabilities": {
                    "max_runner_payload_bytes": 1000,
                    "max_inline_document_bytes": 500,
                },
            },
            project_root=tmp_path,
            server_url="https://simstack.example/api",
        )


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_runner_config_rejects_resource_protocol_drift(tmp_path, mutation):
    resource = {
        "resource_str": "local",
        "workdir": "/tmp/work",
        "hostname": "runner-host",
        "python_paths": [],
        "environment_start": None,
        "is_default": False,
    }
    if mutation == "missing":
        resource.pop("hostname")
    else:
        resource["git_branch"] = "legacy"

    with pytest.raises(ValueError, match="exact v1 DTO"):
        RunnerConfig.from_bootstrap(
            {
                "resource": resource,
                "capabilities": {
                    "max_runner_payload_bytes": 1000,
                    "max_inline_document_bytes": 500,
                },
            },
            project_root=tmp_path,
            server_url="https://simstack.example/api",
        )


def test_runner_task_workdir_uses_only_bounded_task_id(tmp_path):
    workdir = tmp_path / "runner-work"

    result = runner_task_workdir(workdir, "507f1f77bcf86cd799439011")

    assert result == (workdir.resolve() / "tasks" / "507f1f77bcf86cd799439011")


@pytest.mark.parametrize("task_id", ["../escape", "/tmp/escape", "..\\escape"])
def test_runner_task_workdir_rejects_traversal(tmp_path, task_id):
    with pytest.raises(ValueError, match="single path component"):
        runner_task_workdir(tmp_path / "work", task_id)
