import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from simstack.core.context import context
from simstack.tables.table_builder import TableBuilderBase


class RecordingTableBuilder(TableBuilderBase):
    """Concrete TableBuilderBase for tests: records which files were 'processed'."""

    def __init__(self, db=None, write_schema: bool = False):
        super().__init__(db=db, write_schema=write_schema)
        self.processed_files: list[Path] = []

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger("RecordingTableBuilder")

    async def _process_module(self, module, drops: str) -> None:
        # Not needed for these traversal tests.
        return

    async def _process_file(self, file_path: Path, drops: str) -> None:
        # Override to avoid importing modules from files; just record traversal.
        self.processed_files.append(Path(file_path))


@pytest.fixture
def fake_context_project_root(tmp_path, monkeypatch):
    """
    Make TableBuilderBase directory resolution work without running real context initialization.
    """
    old_initialized = getattr(context, "_initialized", False)
    old_config = getattr(context, "config", None)

    context._initialized = True
    context.config = SimpleNamespace(project_root=tmp_path)

    yield tmp_path

    context.config = old_config
    context._initialized = old_initialized


@pytest.mark.asyncio
async def test_iter_python_files_under_dir_recurses_and_skips_common_excludes(
    tmp_path, fake_context_project_root, monkeypatch
):
    # Build a directory tree with some excluded directories
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y = 2")

    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.py").write_text("should_not_be_seen = True")

    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "d.py").write_text("should_not_be_seen = True")

    builder = RecordingTableBuilder()

    found = list(builder._iter_python_files_under_dir(tmp_path, exclude=[]))
    found_rel = sorted(p.relative_to(tmp_path).as_posix() for p in found)

    assert found_rel == ["__pycache__/c.py", "a.py", "sub/b.py"]


@pytest.mark.asyncio
async def test_process_dirs_resolves_relative_dirs_against_project_root(
    tmp_path, fake_context_project_root, monkeypatch
):
    # project_root/pkg/e.py
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "e.py").write_text("z = 3")

    builder = RecordingTableBuilder()

    # Pass a *relative* directory; TableBuilderBase should resolve it against context.config.project_root
    await builder._process_dirs([Path("pkg")], drops="", exclude=[])

    processed_rel = sorted(
        p.relative_to(tmp_path).as_posix() for p in builder.processed_files
    )
    assert processed_rel == ["pkg/e.py"]


@pytest.mark.asyncio
async def test_iter_python_files_under_dir_accepts_single_python_file(
    tmp_path, fake_context_project_root, monkeypatch
):
    single = tmp_path / "single.py"
    single.write_text("value = 42")

    builder = RecordingTableBuilder()

    found = list(builder._iter_python_files_under_dir(single, exclude=[]))
    assert found == [single]
