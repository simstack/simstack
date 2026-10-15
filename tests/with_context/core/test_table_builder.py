import logging
from pathlib import Path

import pytest

from simstack.tables.table_builder import TableBuilderBase


class RecordingTableBuilder(TableBuilderBase):
    """Concrete TableBuilderBase for tests: records which files were 'processed'."""

    def __init__(
        self,
        repository,
        write_schema: bool = False,
        project_root: Path | None = None,
    ):
        super().__init__(
            repository=repository,
            write_schema=write_schema,
            project_root=project_root,
        )
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


@pytest.mark.asyncio
async def test_iter_python_files_under_dir_recurses_and_skips_common_excludes(
    tmp_path, repository
):
    # Build a directory tree with some excluded directories
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y = 2")

    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.py").write_text("should_not_be_seen = True")

    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "d.py").write_text("should_not_be_seen = True")

    builder = RecordingTableBuilder(repository)

    found = list(builder._iter_python_files_under_dir(tmp_path, exclude=[]))
    found_rel = sorted(p.relative_to(tmp_path).as_posix() for p in found)

    assert found_rel == ["__pycache__/c.py", "a.py", "sub/b.py"]


@pytest.mark.asyncio
async def test_process_dirs_resolves_relative_dirs_against_project_root(
    tmp_path, repository
):
    # project_root/pkg/e.py
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "e.py").write_text("z = 3")

    builder = RecordingTableBuilder(repository, project_root=tmp_path)

    # Relative directories resolve only against the explicitly supplied root.
    await builder._process_dirs([Path("pkg")], drops="", exclude=[])

    processed_rel = sorted(
        p.relative_to(tmp_path).as_posix() for p in builder.processed_files
    )
    assert processed_rel == ["pkg/e.py"]


@pytest.mark.asyncio
async def test_iter_python_files_under_dir_accepts_single_python_file(
    tmp_path, repository
):
    single = tmp_path / "single.py"
    single.write_text("value = 42")

    builder = RecordingTableBuilder(repository)

    found = list(builder._iter_python_files_under_dir(single, exclude=[]))
    assert found == [single]
