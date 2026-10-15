from __future__ import annotations

import logging
from pathlib import Path
import pytest

from simstack.tables.table_builder import TableBuilderBase
from tests.support.in_memory_repository import InMemoryRepository


class RecordingBuilder(TableBuilderBase):
    @property
    def logger(self):
        return logging.getLogger("RegistrationTest")

    async def _process_module(self, module, drops):
        return None

    async def _process_dirs(self, dirs, *, drops, exclude):
        self.directories = dirs


@pytest.mark.asyncio
async def test_registration_without_dirs_does_not_scan_current_directory(tmp_path):
    builder = RecordingBuilder(InMemoryRepository(), project_root=tmp_path)
    await builder.build(dirs=[], ignore_entrypoints=True)
    assert builder.directories == []


@pytest.mark.asyncio
async def test_registration_explicit_dirs_override_configured_legacy_dirs(tmp_path):
    (tmp_path / "config.toml").write_text('active_dirs = ["legacy"]\n')
    builder = RecordingBuilder(InMemoryRepository(), project_root=tmp_path)
    await builder.build(dirs=[Path("explicit")], ignore_entrypoints=True)
    assert builder.directories == [Path("explicit")]


@pytest.mark.asyncio
async def test_non_runner_registration_preserves_configured_active_dirs(tmp_path, caplog):
    (tmp_path / "config.toml").write_text('active_dirs = ["workflow_package"]\n')
    builder = RecordingBuilder(InMemoryRepository(), project_root=tmp_path)
    await builder.build(dirs=[], ignore_entrypoints=True)
    assert builder.directories == [Path("workflow_package")]
    assert "deprecated" in caplog.text
