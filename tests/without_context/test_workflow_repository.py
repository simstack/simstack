from __future__ import annotations

import hashlib
import io
import subprocess
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from odmantic import ObjectId

from simstack.core import workflow_repository
from simstack.core.workflow_repository import cached_repository_checkout
from simstack.models.workflow_repository import CodeSource, WorkflowRepo
from simstack.util import importer


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repository: Path, value: int) -> str:
    (repository / "workflow.py").write_text(
        f"VALUE = {value}\n", encoding="utf-8"
    )
    _git(repository, "add", "workflow.py")
    _git(
        repository,
        "-c",
        "user.name=SimStack",
        "-c",
        "user.email=simstack@example.invalid",
        "commit",
        "-m",
        f"value {value}",
    )
    return _git(repository, "rev-parse", "HEAD")


def _archive(repository: Path) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(repository.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(repository).as_posix())
    return output.getvalue()


class _RepositoryDatabase:
    def __init__(self, repository: WorkflowRepo) -> None:
        self.repository = repository

    async def find_one(self, *_args, **_kwargs):
        return self.repository


@pytest.mark.parametrize(
    "unsafe_path",
    [
        r"..\escape.py",
        r"\\server\share\escape.py",
        r"C:\escape.py",
        "C:/escape.py",
        "C:escape.py",
        "//server/share/escape.py",
    ],
)
def test_repository_paths_reject_windows_escapes(unsafe_path: str):
    with pytest.raises(ValueError, match="unsafe repository path"):
        workflow_repository._safe_path(unsafe_path)


def test_archive_entry_limit_counts_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("first/", b"")
        archive.writestr("second/", b"")
    payload = output.getvalue()
    monkeypatch.setattr(workflow_repository, "_MAX_FILES", 1)

    with pytest.raises(
        ValueError, match="repository archive exceeds extraction limits"
    ):
        workflow_repository._extract_archive(
            payload,
            hashlib.sha256(payload).hexdigest(),
            tmp_path / "extracted",
        )


@pytest.mark.asyncio
async def test_pinned_commit_survives_repository_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_WORKFLOW_CACHE_DIR", str(tmp_path / "cache"))
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    first_commit = _commit(source, 1)
    first_archive = _archive(source)
    repository = WorkflowRepo(
        name="history",
        archive_bytes=first_archive,
        archive_sha256=hashlib.sha256(first_archive).hexdigest(),
        head_commit=first_commit,
    )
    database = _RepositoryDatabase(repository)

    first_checkout = await cached_repository_checkout(
        database, CodeSource(repo_id=repository.id, commit=first_commit)
    )
    assert (first_checkout / "workflow.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    cached_source = first_checkout / "workflow.py"
    cached_source.chmod(0o600)
    cached_source.write_text("VALUE = 999\n", encoding="utf-8")

    restored_checkout = await cached_repository_checkout(
        database, CodeSource(repo_id=repository.id, commit=first_commit)
    )
    assert (restored_checkout / "workflow.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    second_commit = _commit(source, 2)
    replacement_archive = _archive(source)
    repository.archive_bytes = replacement_archive
    repository.archive_sha256 = hashlib.sha256(replacement_archive).hexdigest()
    repository.head_commit = second_commit

    pinned_checkout = await cached_repository_checkout(
        database, CodeSource(repo_id=repository.id, commit=first_commit)
    )
    assert (pinned_checkout / "workflow.py").read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.asyncio
async def test_task_source_pins_only_same_repository_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class OldRepositoryModel:
        pass

    class LegacyModel:
        pass

    class CrossRepositoryModel:
        pass

    class RemovedRepositoryModel:
        pass

    class ReclaimedRepositoryModel:
        pass

    database = AsyncMock()
    database.find_one.return_value = None
    task_source = CodeSource(repo_id=ObjectId(), commit="1" * 40)
    current_same_repository_source = CodeSource(
        repo_id=task_source.repo_id,
        commit="2" * 40,
    )
    cross_repository_source = CodeSource(repo_id=ObjectId(), commit="3" * 40)
    find_mapping = AsyncMock(
        side_effect=[
            SimpleNamespace(
                mapping="current_location.RepositoryModel",
                code_source=current_same_repository_source,
            ),
            SimpleNamespace(
                mapping="legacy_models.LegacyModel",
                code_source=None,
            ),
            SimpleNamespace(
                mapping="cross_repo_models.CrossRepositoryModel",
                code_source=cross_repository_source,
            ),
            None,
            SimpleNamespace(
                mapping="reclaimed_location.ReclaimedRepositoryModel",
                code_source=cross_repository_source,
            ),
        ]
    )
    repository_import = AsyncMock(
        side_effect=[
            OldRepositoryModel,
            CrossRepositoryModel,
            RemovedRepositoryModel,
            ReclaimedRepositoryModel,
        ]
    )
    regular_import = MagicMock(return_value=SimpleNamespace(LegacyModel=LegacyModel))
    (tmp_path / "reclaimed_location.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(importer, "_find_model_mapping", find_mapping)
    monkeypatch.setattr(
        importer,
        "cached_repository_checkout",
        AsyncMock(return_value=tmp_path),
    )
    monkeypatch.setattr(importer, "import_workflow_symbol", repository_import)
    monkeypatch.setattr(importer.importlib, "import_module", regular_import)

    assert (
        await importer.import_class(
            "old_location.RepositoryModel",
            database,
            code_source=task_source,
        )
        is OldRepositoryModel
    )
    assert (
        await importer.import_class(
            "legacy_models.LegacyModel",
            database,
            code_source=task_source,
        )
        is LegacyModel
    )
    assert (
        await importer.import_class(
            "cross_repo_models.CrossRepositoryModel",
            database,
            code_source=task_source,
        )
        is CrossRepositoryModel
    )
    assert (
        await importer.import_class(
            "removed_location.RemovedRepositoryModel",
            database,
            code_source=task_source,
        )
        is RemovedRepositoryModel
    )
    assert (
        await importer.import_class(
            "reclaimed_location.ReclaimedRepositoryModel",
            database,
            code_source=task_source,
        )
        is ReclaimedRepositoryModel
    )
    assert repository_import.await_args_list == [
        call(database, "old_location.RepositoryModel", task_source),
        call(
            database,
            "cross_repo_models.CrossRepositoryModel",
            cross_repository_source,
        ),
        call(database, "removed_location.RemovedRepositoryModel", task_source),
        call(database, "reclaimed_location.ReclaimedRepositoryModel", task_source),
    ]
    regular_import.assert_called_once_with("legacy_models")


@pytest.mark.asyncio
async def test_present_pinned_model_import_failure_does_not_use_other_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    task_source = CodeSource(repo_id=ObjectId(), commit="1" * 40)
    other_source = CodeSource(repo_id=ObjectId(), commit="2" * 40)
    database = AsyncMock()
    (tmp_path / "pinned_models.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        importer,
        "_find_model_mapping",
        AsyncMock(
            return_value=SimpleNamespace(
                mapping="other_models.PinnedModel",
                code_source=other_source,
            )
        ),
    )
    monkeypatch.setattr(
        importer,
        "cached_repository_checkout",
        AsyncMock(return_value=tmp_path),
    )
    repository_import = AsyncMock(side_effect=ImportError("missing dependency"))
    monkeypatch.setattr(importer, "import_workflow_symbol", repository_import)
    regular_import = MagicMock()
    monkeypatch.setattr(importer.importlib, "import_module", regular_import)

    with pytest.raises(ImportError, match="missing dependency"):
        await importer.import_class(
            "pinned_models.PinnedModel",
            database,
            code_source=task_source,
        )

    repository_import.assert_awaited_once_with(
        database,
        "pinned_models.PinnedModel",
        task_source,
    )
    regular_import.assert_not_called()


@pytest.mark.asyncio
async def test_repository_cache_rejects_unsafe_git_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_WORKFLOW_CACHE_DIR", str(tmp_path / "cache"))
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    head_commit = _commit(source, 1)
    _git(source, "config", "core.sshCommand", "malicious-command")
    archive = _archive(source)
    repository = WorkflowRepo(
        name="unsafe-config",
        archive_bytes=archive,
        archive_sha256=hashlib.sha256(archive).hexdigest(),
        head_commit=head_commit,
    )

    with pytest.raises(ValueError, match="server-controlled or external Git behavior"):
        await cached_repository_checkout(
            _RepositoryDatabase(repository),
            CodeSource(repo_id=repository.id, commit=head_commit),
        )


@pytest.mark.asyncio
async def test_repository_cache_rejects_promisor_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_WORKFLOW_CACHE_DIR", str(tmp_path / "cache"))
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    head_commit = _commit(source, 1)
    (source / ".git" / "objects" / "pack" / "untrusted.promisor").touch()
    archive = _archive(source)
    repository = WorkflowRepo(
        name="partial-clone",
        archive_bytes=archive,
        archive_sha256=hashlib.sha256(archive).hexdigest(),
        head_commit=head_commit,
    )

    with pytest.raises(ValueError, match="partial-clone"):
        await cached_repository_checkout(
            _RepositoryDatabase(repository),
            CodeSource(repo_id=repository.id, commit=head_commit),
        )


def test_git_output_is_bounded(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _commit(source, 1)

    with pytest.raises(ValueError, match="stdout exceeded"):
        workflow_repository._git(
            source,
            "rev-parse",
            "HEAD",
            max_stdout_bytes=8,
        )


def test_cache_lock_serializes_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_WORKFLOW_CACHE_DIR", str(tmp_path / "cache"))
    attempted = threading.Event()
    acquired = threading.Event()

    def contend_for_lock() -> None:
        attempted.set()
        with workflow_repository._cache_lock("shared-repository"):
            acquired.set()

    contender = threading.Thread(target=contend_for_lock)
    try:
        with workflow_repository._cache_lock("shared-repository"):
            contender.start()
            assert attempted.wait(timeout=1)
            assert not acquired.wait(timeout=0.1)
        assert acquired.wait(timeout=1)
    finally:
        contender.join(timeout=1)
    assert not contender.is_alive()
