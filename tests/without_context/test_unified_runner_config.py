"""One downloaded config binds API identity and portable execution defaults."""

from __future__ import annotations

import importlib
import sys
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from simstack.runner_api.config import RunnerConnectionSettings, render_runner_config
from simstack.runner_api.errors import RunnerApiConfigurationError
from simstack.runner_runtime.execution_adapter import RunnerExecutionAdapterConfig


def write_config(tmp_path, resource="local", server_url="https://example.test/api"):
    path = tmp_path / "config.toml"
    path.write_text(render_runner_config(server_url, resource))
    return path


def cli_args(**updates):
    values = dict(
        resource=None,
        config=None,
        connection_config=None,
        project_root=None,
        workdir=None,
        with_file_transfer=True,
        allow_environment=[],
        polling_interval=20,
        timeout=None,
    )
    return SimpleNamespace(**(values | updates))


def fake_runner(monkeypatch, resource="local"):
    module = importlib.import_module("simstack.core.runner")
    context = SimpleNamespace(
        initialize_runner=AsyncMock(),
        config=SimpleNamespace(resource=resource, is_default=True),
    )
    manager = SimpleNamespace(run_nodes_for_resource=AsyncMock())
    monkeypatch.setattr(module, "context", context)
    monkeypatch.setattr(module, "RunnerManager", Mock(return_value=manager))
    monkeypatch.setattr(module, "sanitize_runner_process_environment", Mock())
    monkeypatch.setattr(module, "discard_runner_token_from_environment", Mock())
    return module, context, manager


@pytest.mark.parametrize(
    "resource", ["local", "runner", "execution", 'quoted"resource']
)
def test_downloaded_config_is_complete_and_portable(tmp_path, resource):
    path = write_config(tmp_path, resource)
    settings = RunnerConnectionSettings.from_file(path)
    assert settings.resource == resource
    assert settings.server_url == "https://example.test/api"
    document = tomllib.loads(path.read_text())
    assert set(document) == {"runner", "execution"}
    assert set(document["runner"]) == {"server_url", "resource"}
    adapter = RunnerExecutionAdapterConfig.from_file(path, resource)
    assert adapter.workflow_installer("python").base_python == sys.executable
    assert adapter.local("python").worker_command[0] == sys.executable
    assert (
        adapter.local("anything", runtime_profile="python").worker_command[0]
        == "{python}"
    )
    assert adapter.worker_environment("anything", runtime_profile="python") == ()
    with pytest.raises(RuntimeError, match="local_worker_command"):
        adapter.local("an_undeclared_program")


def test_execution_config_cannot_override_token_bound_resource(tmp_path):
    path = write_config(tmp_path)
    with pytest.raises(RuntimeError, match="does not match"):
        RunnerExecutionAdapterConfig.from_file(path, "another-resource")


@pytest.mark.parametrize("extra", ["token", "runner_token", "database_uri"])
def test_runner_connection_rejects_secret_fields_without_echoing_them(tmp_path, extra):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[runner]\nserver_url="https://example.test/api"\nresource="local"\n{extra}="private-value"\n'
    )
    with pytest.raises(RunnerApiConfigurationError) as error:
        RunnerConnectionSettings.from_file(path)
    assert "private-value" not in str(error.value)


def test_unified_config_rejects_legacy_database_sections(tmp_path):
    path = write_config(tmp_path)
    path.write_text(
        path.read_text() + '[parameters.common]\nconnection_string="private-value"\n'
    )
    with pytest.raises(RunnerApiConfigurationError, match="legacy database"):
        RunnerConnectionSettings.from_file(path)


@pytest.mark.asyncio
async def test_single_config_initializes_bound_connection_and_execution(
    monkeypatch, tmp_path
):
    module, context, manager = fake_runner(monkeypatch)
    path = write_config(tmp_path)
    monkeypatch.setenv("SIMSTACK_SERVER_URL", "https://wrong-server.test/api")
    await module.async_main(cli_args(config=str(path)))
    assert (
        context.initialize_runner.await_args.kwargs["server_url"]
        == "https://example.test/api"
    )
    assert context.initialize_runner.await_args.kwargs["resource"] == "local"
    assert context.initialize_runner.await_args.kwargs["config_file"] == str(path)
    manager.run_nodes_for_resource.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_config_in_project_root_is_used(monkeypatch, tmp_path):
    module, context, _ = fake_runner(monkeypatch)
    path = write_config(tmp_path)
    await module.async_main(cli_args(project_root=str(tmp_path)))
    assert context.initialize_runner.await_args.kwargs["config_file"] == str(path)
    assert (
        context.initialize_runner.await_args.kwargs["server_url"]
        == "https://example.test/api"
    )


@pytest.mark.asyncio
async def test_legacy_execution_only_config_keeps_environment_connection(
    monkeypatch, tmp_path
):
    module, context, _ = fake_runner(monkeypatch)
    path = tmp_path / "execution.toml"
    path.write_text('[local.program.python]\nlocal_worker_command=["custom-python"]\n')
    await module.async_main(cli_args(config=str(path)))
    assert "server_url" not in context.initialize_runner.await_args.kwargs
    assert context.initialize_runner.await_args.kwargs["config_file"] == str(path)


@pytest.mark.asyncio
async def test_legacy_split_connection_is_still_supported(monkeypatch, tmp_path):
    module, context, _ = fake_runner(monkeypatch)
    path = tmp_path / "runner.toml"
    path.write_text(
        '[runner]\nserver_url="https://example.test/api"\nresource="local"\n'
    )
    execution = tmp_path / "execution.toml"
    execution.write_text("[local]\n")
    await module.async_main(
        cli_args(config=str(execution), connection_config=str(path))
    )
    assert (
        context.initialize_runner.await_args.kwargs["server_url"]
        == "https://example.test/api"
    )
    assert context.initialize_runner.await_args.kwargs["config_file"] == str(execution)


@pytest.mark.asyncio
async def test_conflicting_connection_file_is_not_used_as_fallback(
    monkeypatch, tmp_path
):
    module, context, _ = fake_runner(monkeypatch)
    unified = write_config(tmp_path)
    other = tmp_path / "runner.toml"
    other.write_text(
        '[runner]\nserver_url="https://other.test/api"\nresource="local"\n'
    )
    with pytest.raises(RunnerApiConfigurationError, match="conflict"):
        await module.async_main(
            cli_args(config=str(unified), connection_config=str(other))
        )
    context.initialize_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_resource_mismatch_fails_before_bootstrap(monkeypatch, tmp_path):
    module, context, _ = fake_runner(monkeypatch)
    path = write_config(tmp_path)
    with pytest.raises(RunnerApiConfigurationError, match="does not match"):
        await module.async_main(cli_args(config=str(path), resource="other"))
    context.initialize_runner.assert_not_awaited()


def test_legacy_resource_called_runner_is_not_a_connection(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[runner.program.python]\nlocal_worker_command=["custom-python"]\n')
    assert RunnerConnectionSettings.from_execution_file(path) is None
    adapter = RunnerExecutionAdapterConfig.from_file(path, "runner")
    with pytest.raises(RuntimeError, match="worker_network_guard_command"):
        adapter.local("python")


def test_sample_generator_writes_complete_config_and_never_overwrites(
    monkeypatch, tmp_path
):
    from simstack.util.sample_config import sample_config_writer_entrypoint

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sample-config",
            "--server-url",
            "https://example.test/api",
            "--resource",
            "local",
        ],
    )
    sample_config_writer_entrypoint()
    path = tmp_path / "config.toml"
    assert RunnerConnectionSettings.from_file(path).resource == "local"
    assert (
        RunnerExecutionAdapterConfig.from_file(path, "local")
        .workflow_installer("python")
        .base_python
        == sys.executable
    )
    original = path.read_bytes()
    with pytest.raises(SystemExit):
        sample_config_writer_entrypoint()
    assert path.read_bytes() == original


def test_config_directory_resolves_the_same_single_file(tmp_path):
    write_config(tmp_path)
    assert RunnerConnectionSettings.from_file(tmp_path).resource == "local"
    assert (
        RunnerExecutionAdapterConfig.from_file(tmp_path, "local")
        .workflow_installer("python")
        .base_python
        == sys.executable
    )


@pytest.mark.parametrize("resource", ["execution", "runner"])
def test_legacy_reserved_resource_names_remain_execution_tables(tmp_path, resource):
    path = tmp_path / "config.toml"
    profile = (
        'local_worker_command=["custom-python"]\n'
        'worker_network_guard_command=["operator-guard"]\n'
        "worker_environment_allowlist=[]\n"
    )
    path.write_text(
        "[execution.program.python]\n" + profile + "[runner.program.python]\n" + profile
    )
    assert RunnerConnectionSettings.from_execution_file(path) is None
    adapter = RunnerExecutionAdapterConfig.from_file(path, resource)
    assert adapter.local("python").guarded_worker_command == (
        "operator-guard",
        "custom-python",
    )
