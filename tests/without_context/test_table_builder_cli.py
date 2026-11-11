import asyncio
import logging
import sys
from pathlib import Path

import pytest

from simstack.tables.table_builder import TableBuilderBase
from simstack.tables.catalogue import CatalogueCollector


@pytest.fixture(autouse=True)
def preserve_event_loop():
    policy = asyncio.get_event_loop_policy()
    try:
        previous_loop = policy.get_event_loop()
    except RuntimeError:
        previous_loop = None
    previous_level = logging.getLogger("RecordingCliBuilder").level
    yield
    policy.set_event_loop(previous_loop)
    logging.getLogger("RecordingCliBuilder").setLevel(previous_level)


class RecordingCliBuilder(TableBuilderBase):
    captured: dict = {}

    @property
    def logger(self):
        return logging.getLogger("RecordingCliBuilder")

    async def _process_module(self, module, drops):
        raise AssertionError("CLI argument tests must not import workflow modules")

    async def clear_table(self):
        self.catalogue.models.clear()
        self.catalogue.nodes.clear()

    async def build(self, **kwargs):
        type(self).captured = kwargs


def test_collector_cannot_be_replaced_by_a_runner_client():
    with pytest.raises(TypeError, match="CatalogueCollector"):
        RecordingCliBuilder(object())


def test_cli_omitted_dir_passes_empty_dirs(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["create_model_table"])
    TableBuilderBase.cli_main(RecordingCliBuilder)
    assert RecordingCliBuilder.captured["dirs"] == []
    assert '"models": []' in capsys.readouterr().out


def test_cli_dir_is_resolved_against_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["create_model_table", "--dir", "pkg"])
    TableBuilderBase.cli_main(RecordingCliBuilder)
    assert RecordingCliBuilder.captured["dirs"] == [tmp_path / "pkg"]


@pytest.mark.parametrize("flag,level", [("-v", logging.INFO), ("-vv", logging.DEBUG)])
def test_cli_verbosity_sets_discovery_logger_level(monkeypatch, flag, level):
    monkeypatch.setattr(sys, "argv", ["create_model_table", flag])
    TableBuilderBase.cli_main(RecordingCliBuilder)
    assert logging.getLogger("RecordingCliBuilder").level == level
