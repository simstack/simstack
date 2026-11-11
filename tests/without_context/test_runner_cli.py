from types import SimpleNamespace
from unittest.mock import AsyncMock
import os
import pytest
from simstack.core import runner


@pytest.fixture(autouse=True)
def restore_machine_config(monkeypatch):
    # Register the CLI's direct environment write for teardown, including when
    # the variable was originally absent.
    monkeypatch.setenv("SIMSTACK_MACHINE_CONFIG", os.environ.get("SIMSTACK_MACHINE_CONFIG", ""))


def test_runner_cli_rejects_pull_without_starting_runtime(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["simstack_runner", "--pull", "true"])
    start = AsyncMock()
    monkeypatch.setattr(runner, "async_main", start)
    with pytest.raises(SystemExit) as error:
        runner.runner_main()
    assert error.value.code == 2
    assert "publish a repository revision through Server" in capsys.readouterr().err
    start.assert_not_awaited()


def test_runner_help_does_not_advertise_git_pull(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["simstack_runner", "--help"])
    with pytest.raises(SystemExit) as error:
        runner.runner_main()
    assert error.value.code == 0
    assert "--pull" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_async_main_uses_server_catalogue_without_local_registration(
    monkeypatch
):
    captured: dict[str, object] = {}

    class DummyContext:
        def __init__(self):
            self.config = SimpleNamespace(resource="docker")

        async def initialize(self, **kwargs):
            self.config = SimpleNamespace(resource=kwargs.get("resource"))

    class DummyRunnerManager:
        def __init__(
            self,
            resource,
            *,
            detach,
            no_pull,
            is_default,
            with_file_transfer=True,
            with_jobs=True,
        ):
            captured["resource"] = resource
            captured["detach"] = detach
            captured["no_pull"] = no_pull
            captured["is_default"] = is_default
            captured["with_file_transfer"] = with_file_transfer
            captured["with_jobs"] = with_jobs

        async def run_nodes_for_resource(self, polling_interval, *_args, timeout=None):
            captured["polling_interval"] = polling_interval
            captured["timeout"] = timeout

    monkeypatch.setattr(runner, "context", DummyContext())
    monkeypatch.setattr(runner, "RunnerManager", DummyRunnerManager)

    args = SimpleNamespace(
        resource="docker",
        detach=True,
        pull=True,
        polling_interval=5,
        timeout=None,
        config="simstack.toml",
        file_transfer=False,
        jobs=True,
    )

    await runner.async_main(args)

    assert captured["is_default"] is False
    assert captured["resource"] == "docker"
    assert captured["with_jobs"] is True
    assert captured["with_file_transfer"] is False


@pytest.mark.asyncio
async def test_async_main_passes_with_jobs_false(monkeypatch):
    captured: dict[str, object] = {}

    class DummyContext:
        def __init__(self):
            self.config = SimpleNamespace(resource="cloud")

        async def initialize(self, **kwargs):
            self.config = SimpleNamespace(resource=kwargs.get("resource"))

    class DummyRunnerManager:
        def __init__(
            self,
            resource,
            *,
            detach,
            no_pull,
            is_default,
            with_file_transfer=True,
            with_jobs=True,
        ):
            captured["resource"] = resource
            captured["with_file_transfer"] = with_file_transfer
            captured["with_jobs"] = with_jobs
            captured["no_pull"] = no_pull

        async def run_nodes_for_resource(self, polling_interval, *_args, timeout=None):
            captured["polling_interval"] = polling_interval

    monkeypatch.setattr(runner, "context", DummyContext())
    monkeypatch.setattr(runner, "RunnerManager", DummyRunnerManager)

    args = SimpleNamespace(
        resource="cloud",
        detach=True,
        pull=False,
        polling_interval=20,
        timeout=None,
        config="simstack.toml",
        file_transfer=True,
        jobs=False,
    )

    await runner.async_main(args)

    assert captured["with_jobs"] is False
    assert captured["with_file_transfer"] is True
    assert captured["no_pull"] is True
    assert captured["resource"] == "cloud"
