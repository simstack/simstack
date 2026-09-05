from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core import runner


@pytest.mark.asyncio
async def test_initialize_default_resource_returns_none_when_resource_is_missing(
    tmp_path, monkeypatch
):
    db = SimpleNamespace(find_one=AsyncMock(return_value=None))
    config = SimpleNamespace(resource="docker", project_root=tmp_path)
    monkeypatch.setattr(runner, "context", SimpleNamespace(db=db, config=config))

    result = await runner.initialize_default_resource()

    assert result is None


@pytest.mark.asyncio
async def test_initialize_default_resource_keeps_resource_when_config_toml_missing(
    tmp_path, monkeypatch
):
    resource_def = SimpleNamespace(is_default=True)
    db = SimpleNamespace(
        find_one=AsyncMock(return_value=resource_def), core_engine=object()
    )
    config = SimpleNamespace(resource="docker", project_root=tmp_path)

    make_node_table = AsyncMock()
    make_model_table = AsyncMock()
    monkeypatch.setattr(runner, "context", SimpleNamespace(db=db, config=config))
    monkeypatch.setattr(runner, "make_node_table", make_node_table)
    monkeypatch.setattr(runner, "make_model_table", make_model_table)

    result = await runner.initialize_default_resource()

    assert result is resource_def
    make_node_table.assert_not_called()
    make_model_table.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_default_resource_builds_model_table_before_node_table(
    tmp_path, monkeypatch
):
    resource_def = SimpleNamespace(is_default=True)
    engine = object()
    db = SimpleNamespace(
        find_one=AsyncMock(return_value=resource_def), core_engine=engine
    )
    config = SimpleNamespace(resource="docker", project_root=tmp_path)
    (tmp_path / "config.toml").write_text('active_dirs = ["src/simstack/models"]\n')
    call_order = []

    async def make_model_table(*args, **kwargs):
        call_order.append(("model", args, kwargs))

    async def make_node_table(*args, **kwargs):
        call_order.append(("node", args, kwargs))

    monkeypatch.setattr(runner, "context", SimpleNamespace(db=db, config=config))
    monkeypatch.setattr(runner, "make_model_table", make_model_table)
    monkeypatch.setattr(runner, "make_node_table", make_node_table)

    result = await runner.initialize_default_resource()

    assert result is resource_def
    assert [call[0] for call in call_order] == ["model", "node"]
    assert call_order[0][1] == (db,)
    assert call_order[1][1] == (db,)
    assert call_order[0][2] == {"dirs": ["src/simstack/models"]}
    assert call_order[1][2] == {"dirs": ["src/simstack/models"]}


@pytest.mark.asyncio
async def test_async_main_uses_false_is_default_when_default_resource_init_returns_none(
    monkeypatch
):
    captured: dict[str, object] = {}

    class DummyContext:
        def __init__(self):
            self.config = SimpleNamespace(resource="docker")

        async def initialize(self, **kwargs):
            self.config = SimpleNamespace(resource=kwargs.get("resource"))

    class DummyRunnerManager:
        def __init__(self, resource, *, detach, no_pull, is_default, with_file_transfer=True):
            captured["resource"] = resource
            captured["detach"] = detach
            captured["no_pull"] = no_pull
            captured["is_default"] = is_default
            captured["with_file_transfer"] = with_file_transfer

        async def run_nodes_for_resource(self, polling_interval, *_args, timeout=None):
            captured["polling_interval"] = polling_interval
            captured["timeout"] = timeout

    monkeypatch.setattr(runner, "context", DummyContext())
    monkeypatch.setattr(
        runner, "initialize_default_resource", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(runner, "RunnerManager", DummyRunnerManager)

    args = SimpleNamespace(
        resource="docker",
        db_name=None,
        detach=True,
        pull=True,
        polling_interval=5,
        timeout=None,
        connection_string="none",
        config="simstack.toml",
        file_transfer=False,
    )

    await runner.async_main(args)

    assert captured["is_default"] is False
    assert captured["resource"] == "docker"
