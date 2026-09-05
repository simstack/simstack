import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from simstack.tables.table_builder import TableBuilderBase


class RecordingCliBuilder(TableBuilderBase):
    captured: dict = {}

    def __init__(self, db=None, write_schema: bool = False, project_root=None):
        super().__init__(db=db, write_schema=write_schema, project_root=project_root)

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger("RecordingCliBuilder")

    async def _process_module(self, module, drops: str) -> None:
        return

    async def build(self, **kwargs):
        type(self).captured = kwargs

    async def second_stage(self, drops: str) -> None:
        return


def test_cli_omitted_dir_passes_empty_dirs(monkeypatch):
    RecordingCliBuilder.captured = {}

    async def fake_initialize(**kwargs):
        return None

    monkeypatch.setattr(
        "simstack.tables.table_builder.context",
        SimpleNamespace(initialize=fake_initialize, db=object()),
    )
    monkeypatch.setattr(sys, "argv", ["create_model_table"])

    TableBuilderBase.cli_main(RecordingCliBuilder)

    assert RecordingCliBuilder.captured["dirs"] == []


def test_cli_dir_is_resolved_against_cwd(monkeypatch, tmp_path):
    RecordingCliBuilder.captured = {}

    async def fake_initialize(**kwargs):
        return None

    monkeypatch.setattr(
        "simstack.tables.table_builder.context",
        SimpleNamespace(initialize=fake_initialize, db=object()),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["create_model_table", "--dir", "pkg"])

    TableBuilderBase.cli_main(RecordingCliBuilder)

    assert RecordingCliBuilder.captured["dirs"] == [tmp_path / "pkg"]


def test_cli_v_sets_info_log_level(monkeypatch):
    captured = {}

    async def fake_initialize(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "simstack.tables.table_builder.context",
        SimpleNamespace(initialize=fake_initialize, db=object()),
    )
    monkeypatch.setattr(sys, "argv", ["create_model_table", "-v"])

    TableBuilderBase.cli_main(RecordingCliBuilder)

    assert captured["log_level"] == logging.INFO


def test_cli_vv_sets_debug_log_level(monkeypatch):
    captured = {}

    async def fake_initialize(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "simstack.tables.table_builder.context",
        SimpleNamespace(initialize=fake_initialize, db=object()),
    )
    monkeypatch.setattr(sys, "argv", ["create_model_table", "-vv"])

    TableBuilderBase.cli_main(RecordingCliBuilder)

    assert captured["log_level"] == logging.DEBUG
