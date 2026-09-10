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


class ImportingTableBuilder(TableBuilderBase):
    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger("ImportingTableBuilder")

    async def _process_module(self, module, drops: str) -> None:
        return


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

    processed_rel = sorted(p.relative_to(tmp_path).as_posix() for p in builder.processed_files)
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


@pytest.mark.asyncio
async def test_process_file_propagates_broken_user_module_import(tmp_path):
    package = tmp_path / "user_nodes"
    package.mkdir()
    broken_module = package / "broken.py"
    broken_module.write_text(
        'raise RuntimeError("broken user module")\n', encoding="utf-8"
    )
    builder = ImportingTableBuilder(db=None, project_root=tmp_path)

    with pytest.raises(RuntimeError, match="broken user module"):
        await builder._process_file(broken_module, drops="")


@pytest.mark.asyncio
async def test_process_file_converts_successful_system_exit_to_failure(tmp_path):
    package = tmp_path / "user_nodes"
    package.mkdir()
    broken_module = package / "system_exit.py"
    broken_module.write_text("raise SystemExit(0)\n", encoding="utf-8")
    builder = ImportingTableBuilder(db=None, project_root=tmp_path)

    with pytest.raises(SystemExit) as caught:
        await builder._process_file(broken_module, drops="")

    assert caught.value.code == 1


@pytest.mark.asyncio
async def test_installed_module_import_failure_is_not_skipped(monkeypatch):
    builder = ImportingTableBuilder(db=None)
    monkeypatch.setattr(
        "simstack.tables.table_builder.find_simstack_modules",
        lambda: ["broken_installed_nodes"],
    )

    def fail_import(module_name):
        raise ImportError(f"cannot import {module_name}")

    monkeypatch.setattr(
        "simstack.tables.table_builder.importlib.import_module", fail_import
    )

    with pytest.raises(ImportError, match="cannot import broken_installed_nodes"):
        await builder._process_simstack_modules(drops="")


def test_installed_module_successful_system_exit_is_failure(monkeypatch):
    builder = ImportingTableBuilder(db=None)

    def exit_successfully(module_name):
        raise SystemExit(0)

    monkeypatch.setattr(
        "simstack.tables.table_builder.importlib.import_module", exit_successfully
    )

    with pytest.raises(SystemExit) as caught:
        builder._import_package_module("broken_installed_nodes")

    assert caught.value.code == 1


@pytest.mark.asyncio
async def test_build_without_dirs_does_not_scan_project_root(
    tmp_path, fake_context_project_root
):
    (tmp_path / "stray.py").write_text("x = 1")
    builder = RecordingTableBuilder()

    await builder.build(dirs=None, ignore_entrypoints=True)

    assert builder.processed_files == []


@pytest.mark.asyncio
async def test_build_uses_deprecated_active_dirs_when_dirs_omitted(
    tmp_path, fake_context_project_root, caplog
):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "m.py").write_text("x = 1")
    (tmp_path / "config.toml").write_text('active_dirs = ["legacy"]\n')
    builder = RecordingTableBuilder()

    with caplog.at_level(logging.WARNING, logger="RecordingTableBuilder"):
        await builder.build(dirs=None, ignore_entrypoints=True)

    processed = sorted(p.relative_to(tmp_path).as_posix() for p in builder.processed_files)
    assert processed == ["legacy/m.py"]
    assert "active_dirs in config.toml is deprecated" in caplog.text


@pytest.mark.asyncio
async def test_build_with_explicit_dirs_does_not_use_active_dirs(
    tmp_path, fake_context_project_root
):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "e.py").write_text("x = 1")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "l.py").write_text("x = 1")
    (tmp_path / "config.toml").write_text('active_dirs = ["legacy"]\n')
    builder = RecordingTableBuilder()

    await builder.build(dirs=[Path("explicit")], ignore_entrypoints=True)

    processed = sorted(p.relative_to(tmp_path).as_posix() for p in builder.processed_files)
    assert processed == ["explicit/e.py"]


@pytest.mark.asyncio
async def test_active_dirs_must_be_a_list(tmp_path, fake_context_project_root):
    (tmp_path / "config.toml").write_text('active_dirs = "legacy"\n')
    builder = RecordingTableBuilder()

    with pytest.raises(ValueError, match="must be a list"):
        await builder.build(dirs=None, ignore_entrypoints=True)


@pytest.mark.asyncio
async def test_active_dirs_entries_must_be_strings(tmp_path, fake_context_project_root):
    (tmp_path / "config.toml").write_text("active_dirs = [1]\n")
    builder = RecordingTableBuilder()

    with pytest.raises(ValueError, match="must contain only strings"):
        await builder.build(dirs=None, ignore_entrypoints=True)
