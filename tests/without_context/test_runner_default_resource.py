import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from simstack.core import runner


@pytest.mark.asyncio
async def test_async_main_initializes_api_only_runner(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class DummyContext:
        def __init__(self):
            self.config = SimpleNamespace(resource="runner-resource", is_default=True)

        async def initialize_runner(self, **kwargs):
            captured["initialize"] = kwargs

    class DummyRunnerManager:
        def __init__(
            self,
            resource,
            *,
            is_default,
            with_file_transfer=True,
        ):
            captured["resource"] = resource
            captured["is_default"] = is_default
            captured["with_file_transfer"] = with_file_transfer

        async def run_nodes_for_resource(self, polling_interval, *_args, timeout=None):
            captured["polling_interval"] = polling_interval
            captured["timeout"] = timeout

    monkeypatch.setattr(runner, "context", DummyContext())
    monkeypatch.setattr(runner, "RunnerManager", DummyRunnerManager)
    sanitize = MagicMock()
    discard = MagicMock()
    monkeypatch.setattr(runner, "sanitize_runner_process_environment", sanitize)
    monkeypatch.setattr(runner, "discard_runner_token_from_environment", discard)

    args = SimpleNamespace(
        resource=None,
        project_root=str(tmp_path),
        workdir=str(tmp_path / "runner-work"),
        polling_interval=5,
        timeout=None,
        config="config.toml",
        with_file_transfer=False,
        allow_environment=["PROGRAM_LICENSE"],
    )

    await runner.async_main(args)

    assert captured["initialize"] == {
        "resource": None,
        "config_file": "config.toml",
        "project_root": str(tmp_path),
        "workdir_override": str(tmp_path / "runner-work"),
        "with_file_transfer": False,
    }
    assert captured["resource"] == "runner-resource"
    assert captured["is_default"] is True
    assert captured["with_file_transfer"] is False
    sanitize.assert_called_once_with(allowed_environment_names=("PROGRAM_LICENSE",))
    discard.assert_called_once_with()


@pytest.mark.asyncio
async def test_async_main_cli_workdir_survives_environment_sanitization(
    monkeypatch, tmp_path
):
    captured: dict[str, object] = {}
    explicit_workdir = tmp_path / "explicit-workdir"

    class DummyContext:
        config = SimpleNamespace(resource="runner-resource", is_default=False)

        async def initialize_runner(self, **kwargs):
            captured["initialize"] = kwargs

    class DummyRunnerManager:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run_nodes_for_resource(self, *_args, **_kwargs):
            return None

    def sanitize(*, allowed_environment_names):
        captured["allowed_environment_names"] = allowed_environment_names
        os.environ.pop("SIMSTACK_RUNNER_WORKDIR", None)

    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path / "ambient-workdir"))
    monkeypatch.setattr(runner, "context", DummyContext())
    monkeypatch.setattr(runner, "RunnerManager", DummyRunnerManager)
    monkeypatch.setattr(runner, "sanitize_runner_process_environment", sanitize)
    monkeypatch.setattr(runner, "discard_runner_token_from_environment", lambda: None)

    await runner.async_main(
        SimpleNamespace(
            resource="local-runner",
            project_root=str(tmp_path),
            workdir=str(explicit_workdir),
            polling_interval=1,
            timeout=None,
            config="config.toml",
            with_file_transfer=True,
            allow_environment=[],
        )
    )

    assert "SIMSTACK_RUNNER_WORKDIR" not in os.environ
    assert captured["allowed_environment_names"] == ()
    assert captured["initialize"]["workdir_override"] == str(explicit_workdir)


def test_production_runner_has_no_self_update_lifecycle():
    project_root = Path(__file__).resolve().parents[2]
    explicit_sources = {
        project_root / "src/simstack/core/runner.py",
        project_root / "src/simstack/core/run_node.py",
        project_root / "src/simstack/core/run_docker.py",
        project_root / "src/simstack/core/submit_node.py",
        project_root / "src/simstack/core/node_runner.py",
    }
    package_roots = (
        project_root / "src/simstack/core/services",
        project_root / "src/simstack/core/runner_execution",
        project_root / "src/simstack/runner_api",
        project_root / "src/simstack/runner_runtime",
    )
    production_sources = sorted(
        explicit_sources
        | {
            source
            for package_root in package_roots
            for source in package_root.rglob("*.py")
        }
    )
    forbidden = (
        "GitUvUpdateService",
        "ResourceBranchMonitorService",
        "GitRestartService",
        "--no-pull",
        "user_extra_config.toml",
        "get_git_status",
        '"fetch"',
    )

    for source_path in production_sources:
        source = source_path.read_text(encoding="utf-8")
        assert all(value not in source for value in forbidden)

    assert not (
        project_root / "src/simstack/core/services/git_uv_update_service.py"
    ).exists()
    assert not (
        project_root / "src/simstack/core/services/resource_branch_monitor_service.py"
    ).exists()
    assert not (
        project_root / "src/simstack/core/services/git_restart_service.py"
    ).exists()
