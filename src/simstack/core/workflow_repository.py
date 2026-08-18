from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib
import inspect
import io
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType
from typing import Any

from simstack.models.workflow_repository import CodeSource, WorkflowRepo

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover - platform-specific import
    import fcntl


logger = logging.getLogger("WorkflowRepository")
MAX_WORKFLOW_ARCHIVE_BYTES = 12 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 128 * 1024 * 1024
_MAX_FILES = 4096
_MAX_GIT_METADATA_BYTES = 16 * 1024 * 1024
_MAX_GIT_STDERR_BYTES = 1024 * 1024
_MAX_GIT_CONFIG_BYTES = 64 * 1024


@contextmanager
def _cache_lock(key: str) -> Iterator[None]:
    lock_directory = _cache_root() / ".locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    lock_path = lock_directory / hashlib.sha256(key.encode("utf-8")).hexdigest()
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        lock_file.fileno(),
                        msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                        1,
                    )
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EDEADLK):
                        raise
                    time.sleep(0.05)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(),
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,
                )
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _safe_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"unsafe repository path: {name!r}")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or PureWindowsPath(name).drive
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError(f"unsafe repository path: {name!r}")
    return path


def _extract_archive(archive: bytes, expected_sha256: str, destination: Path) -> None:
    payload = bytes(archive)
    if len(payload) > MAX_WORKFLOW_ARCHIVE_BYTES:
        raise ValueError("repository archive exceeds the 12 MB limit")
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"repository archive SHA-256 mismatch: expected {expected_sha256}, "
            f"got {actual_sha256}"
        )

    try:
        archive_file = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError("repository archive is not a valid ZIP") from exc

    with archive_file:
        members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        seen: set[PurePosixPath] = set()
        total_size = 0
        entry_count = 0
        for member in archive_file.infolist():
            path = _safe_path(member.filename.rstrip("/"))
            if path in seen:
                raise ValueError(f"duplicate repository path: '{path}'")
            seen.add(path)
            entry_count += 1
            mode = member.external_attr >> 16
            if member.flag_bits & 1 or stat.S_IFMT(mode) not in (
                0,
                stat.S_IFREG,
                stat.S_IFDIR,
            ):
                raise ValueError(f"unsupported repository entry: '{path}'")
            if not member.is_dir():
                total_size += member.file_size
            if entry_count > _MAX_FILES or total_size > _MAX_EXTRACTED_BYTES:
                raise ValueError("repository archive exceeds extraction limits")
            members.append((member, path))

        destination.mkdir(parents=True)
        for member, relative_path in members:
            target = destination.joinpath(*relative_path.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive_file.open(member) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > member.file_size:
                        raise ValueError(f"invalid ZIP size for '{relative_path}'")
                    output.write(chunk)
            if written != member.file_size:
                raise ValueError(f"invalid ZIP size for '{relative_path}'")
            target.chmod(0o700 if mode & 0o111 else 0o600)


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "true",
            "LC_ALL": "C",
        }
    )
    return environment


def _run_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    max_stdout_bytes: int,
    stdout_file: Any | None = None,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=_git_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    seen = {"stdout": 0, "stderr": 0}
    exceeded: list[str] = []

    def drain(name: str, stream: Any, limit: int) -> None:
        while chunk := stream.read(64 * 1024):
            remaining = limit - seen[name]
            accepted = chunk[: max(remaining, 0)]
            if name == "stdout" and stdout_file is not None:
                stdout_file.write(accepted)
            else:
                output[name].extend(accepted)
            seen[name] += len(chunk)
            if len(chunk) > remaining:
                exceeded.append(name)
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return

    readers = [
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout, max_stdout_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr, _MAX_GIT_STDERR_BYTES),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        return_code = process.wait(timeout=30)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join()
        raise ValueError("Git operation exceeded the 30 second limit") from exc
    for reader in readers:
        reader.join()
    if exceeded:
        raise ValueError(f"Git {exceeded[0]} exceeded the configured byte limit")
    return subprocess.CompletedProcess(
        command,
        return_code,
        bytes(output["stdout"]),
        bytes(output["stderr"]),
    )


def _validate_repository_git_metadata(repository: Path) -> None:
    git_directory = repository / ".git"
    for forbidden in (
        git_directory / "objects" / "info" / "alternates",
        git_directory / "objects" / "info" / "http-alternates",
        git_directory / "commondir",
        git_directory / "config.worktree",
        git_directory / "shallow",
    ):
        if forbidden.exists():
            raise ValueError(
                f"repository uses unsupported external Git metadata: {forbidden.name}"
            )

    config = git_directory / "config"
    if config.exists() and (
        not config.is_file() or config.stat().st_size > _MAX_GIT_CONFIG_BYTES
    ):
        raise ValueError("repository .git/config is not a bounded regular file")
    if any((git_directory / "objects").rglob("*.promisor")):
        raise ValueError("partial-clone Git repositories are not supported")
    values: dict[str, str] = {}
    try:
        if config.exists():
            listed = _run_bounded_process(
                [
                    "git",
                    "config",
                    "--file",
                    str(config),
                    "--no-includes",
                    "--null",
                    "--list",
                ],
                cwd=repository,
                max_stdout_bytes=_MAX_GIT_CONFIG_BYTES,
            )
            if listed.returncode:
                raise ValueError("repository .git/config could not be read safely")
            for record in listed.stdout.split(b"\x00"):
                if not record:
                    continue
                raw_key, separator, raw_value = record.partition(b"\n")
                if not separator:
                    raise ValueError("repository .git/config could not be read safely")
                values[raw_key.decode("utf-8", errors="strict").lower()] = (
                    raw_value.decode("utf-8", errors="strict").strip().lower()
                )
    except UnicodeDecodeError as exc:
        raise ValueError("repository .git/config must be UTF-8") from exc

    unsafe_prefixes = (
        "alias.",
        "credential.",
        "diff.",
        "filter.",
        "http.",
        "https.",
        "include.",
        "includeif.",
        "merge.",
        "pager.",
        "protocol.",
        "remote.",
        "submodule.",
        "url.",
    )
    unsafe_keys = {
        "core.alternaterefscommand",
        "core.attributesfile",
        "core.fsmonitor",
        "core.gitproxy",
        "core.hookspath",
        "core.sshcommand",
        "core.worktree",
        "extensions.partialclone",
        "extensions.worktreeconfig",
    }
    if any(key in unsafe_keys or key.startswith(unsafe_prefixes) for key in values):
        raise ValueError(
            "repository .git/config contains server-controlled or external Git behavior"
        )

    repository_format = values.get("core.repositoryformatversion", "0")
    file_mode = values.get("core.filemode", "true")
    bare = values.get("core.bare", "false")
    object_format = values.get("extensions.objectformat", "sha1")
    if repository_format not in {"0", "1"} or file_mode not in {"true", "false"}:
        raise ValueError("repository uses an unsupported Git repository format")
    if bare != "false" or object_format not in {"sha1", "sha256"}:
        raise ValueError("repository uses an unsupported Git repository format")
    if object_format == "sha256" and repository_format != "1":
        raise ValueError("repository SHA-256 object format metadata is invalid")

    sanitized = (
        "[core]\n"
        f"\trepositoryformatversion = {repository_format}\n"
        f"\tfilemode = {file_mode}\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
    )
    if object_format == "sha256":
        sanitized += "[extensions]\n\tobjectformat = sha256\n"
    encoded_config = sanitized.encode("utf-8")
    if not config.exists() or config.read_bytes() != encoded_config:
        config.write_bytes(encoded_config)
        config.chmod(0o600)


def _git(
    repository: Path,
    *arguments: str,
    max_stdout_bytes: int = _MAX_GIT_METADATA_BYTES,
    stdout_file: Any | None = None,
) -> bytes:
    _validate_repository_git_metadata(repository)
    process = _run_bounded_process(
        [
            "git",
            f"--git-dir={repository / '.git'}",
            f"--work-tree={repository}",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            *arguments,
        ],
        cwd=repository,
        max_stdout_bytes=max_stdout_bytes,
        stdout_file=stdout_file,
    )
    if process.returncode:
        error = process.stderr.decode("utf-8", errors="replace").strip()[:1000]
        raise ValueError(error or "Git repository validation failed")
    return process.stdout


def _repository_root(extracted: Path) -> Path:
    git_directories = [
        path
        for path in extracted.rglob(".git")
        if path.is_dir() and not path.is_symlink()
    ]
    if len(git_directories) != 1:
        raise ValueError("repository archive must contain exactly one .git directory")
    return git_directories[0].parent


def _head(repository: Path) -> str:
    return _git(repository, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()


def _make_tree_read_only(root: Path) -> None:
    for current, _, filenames in os.walk(root):
        directory = Path(current)
        for filename in filenames:
            path = directory / filename
            mode = path.stat().st_mode
            path.chmod(0o500 if mode & 0o111 else 0o400)
        directory.chmod(0o500)


def _remove_tree(root: Path) -> None:
    if root.is_symlink():
        root.unlink()
        return
    if not root.exists():
        return
    for current, directories, filenames in os.walk(root):
        directory = Path(current)
        directory.chmod(0o700)
        for name in (*directories, *filenames):
            path = directory / name
            if path.is_symlink():
                path.unlink()
            else:
                path.chmod(0o700)
    shutil.rmtree(root)


def _cache_root() -> Path:
    configured = os.environ.get("SIMSTACK_WORKFLOW_CACHE_DIR")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".cache" / "simstack" / "workflow-repositories")
    )


def _cached_repository(repository: WorkflowRepo) -> Path:
    archive = bytes(repository.archive_bytes)
    if hashlib.sha256(archive).hexdigest() != repository.archive_sha256:
        raise ValueError("stored repository archive failed SHA-256 verification")
    entry = _cache_root() / f"{repository.id}-{repository.archive_sha256}"
    root = entry / "repository"
    with _cache_lock(str(entry)):
        if not entry.is_symlink() and root.is_dir() and not root.is_symlink():
            try:
                cached_root = _repository_root(root)
                if cached_root == root and _head(root) == repository.head_commit:
                    os.utime(entry, None)
                    return root
            except (OSError, ValueError):
                logger.warning(
                    "Discarding invalid workflow repository cache",
                    extra={"repo_id": str(repository.id)},
                )

        entry.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{entry.name}.", dir=entry.parent))
        try:
            unpacked = temporary / "unpacked"
            _extract_archive(archive, repository.archive_sha256, unpacked)
            extracted_root = _repository_root(unpacked)
            if _head(extracted_root) != repository.head_commit:
                raise ValueError("repository HEAD does not match head_commit")
            extracted_root.rename(temporary / "repository")
            if unpacked.exists():
                shutil.rmtree(unpacked)
            _make_tree_read_only(temporary / "repository")
            if entry.exists():
                _remove_tree(entry)
            os.replace(temporary, entry)
            logger.info(
                "Workflow repository cache materialized",
                extra={
                    "repo_id": str(repository.id),
                    "archive_sha256": repository.archive_sha256,
                },
            )
            return entry / "repository"
        except Exception:
            logger.exception(
                "Workflow repository cache materialization failed",
                extra={"repo_id": str(repository.id)},
            )
            raise
        finally:
            if temporary.exists():
                _remove_tree(temporary)


def _checkout_matches(
    destination: Path,
    entries: list[tuple[str, str, PurePosixPath, int]],
) -> bool:
    expected_paths = {path.as_posix() for _, _, path, _ in entries}
    actual_paths = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_paths != expected_paths:
        return False
    for mode, object_id, relative_path, expected_size in entries:
        path = destination.joinpath(*relative_path.parts)
        metadata = path.stat()
        if metadata.st_size != expected_size or bool(metadata.st_mode & 0o111) != (
            mode == "100755"
        ):
            return False
        digest = hashlib.sha1() if len(object_id) == 40 else hashlib.sha256()
        digest.update(f"blob {expected_size}\0".encode("ascii"))
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != object_id:
            return False
    return True


def _materialize_commit(repository: Path, destination: Path, commit: str) -> Path:
    with _cache_lock(str(destination)):
        resolved = (
            _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}")
            .decode()
            .strip()
        )
        if resolved != commit:
            raise LookupError(f"repository does not contain pinned commit {commit}")

        entries: list[tuple[str, str, PurePosixPath, int]] = []
        total_size = 0
        for record in _git(
            repository, "ls-tree", "-rz", "-l", "--full-tree", commit
        ).split(b"\x00"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id, raw_size = metadata.decode("ascii").split(
                " ", 3
            )
            path = _safe_path(raw_path.decode("utf-8"))
            if object_type == "commit":
                raise ValueError(
                    f"Git submodules are not supported for workflow repositories: '{path}'"
                )
            if object_type != "blob" or mode == "120000":
                raise ValueError(f"unsupported Git entry: '{path}'")
            size = int(raw_size)
            total_size += size
            if len(entries) >= _MAX_FILES or total_size > _MAX_EXTRACTED_BYTES:
                raise ValueError("repository commit exceeds extraction limits")
            entries.append((mode, object_id, path, size))

        if destination.is_dir() and _checkout_matches(destination, entries):
            os.utime(destination, None)
            return destination
        if destination.exists():
            _remove_tree(destination)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
        )
        try:
            for mode, object_id, path, expected_size in entries:
                target = temporary.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    _git(
                        repository,
                        "cat-file",
                        "blob",
                        object_id,
                        max_stdout_bytes=expected_size,
                        stdout_file=output,
                    )
                if target.stat().st_size != expected_size:
                    raise ValueError(f"failed to materialize Git object {object_id}")
                target.chmod(0o700 if mode == "100755" else 0o600)
            _make_tree_read_only(temporary)
            os.replace(temporary, destination)
            return destination
        finally:
            if temporary.exists():
                _remove_tree(temporary)


async def cached_repository_checkout(db: Any, code_source: CodeSource) -> Path:
    repository = await db.find_one(WorkflowRepo, WorkflowRepo.id == code_source.repo_id)
    if repository is None:
        raise LookupError(f"workflow repository {code_source.repo_id} was not found")
    return await cached_repository_model_checkout(repository, code_source.commit)


async def cached_repository_model_checkout(
    repository: WorkflowRepo,
    commit: str,
) -> Path:
    """Materialize one commit from an already validated repository document."""

    repository_root = await asyncio.to_thread(_cached_repository, repository)
    checkout = (
        _cache_root()
        / f"{repository.id}-{repository.archive_sha256}"
        / "commits"
        / commit
    )
    return await asyncio.to_thread(
        _materialize_commit, repository_root, checkout, commit
    )


def annotate_workflow_module(module: ModuleType, code_source: CodeSource) -> None:
    setattr(module, "_simstack_code_source", code_source)
    for _, member in inspect.getmembers(
        module, lambda value: inspect.isfunction(value) or inspect.isclass(value)
    ):
        if getattr(member, "__module__", None) != module.__name__:
            continue
        setattr(member, "_simstack_code_source", code_source)
        inner = getattr(member, "_inner", None)
        if inspect.isfunction(inner):
            setattr(inner, "_simstack_code_source", code_source)


def prepare_repository_import(repository_root: Path, module_path: str) -> None:
    root = str(repository_root)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    top_level = module_path.split(".", 1)[0]
    loaded = sys.modules.get(top_level)
    if loaded is not None:
        loaded_paths = [
            getattr(loaded, "__file__", None),
            *getattr(loaded, "__path__", ()),
        ]
        if not any(
            path and Path(path).resolve().is_relative_to(repository_root.resolve())
            for path in loaded_paths
        ):
            for name in tuple(sys.modules):
                if name == top_level or name.startswith(top_level + "."):
                    del sys.modules[name]
    importlib.invalidate_caches()


async def import_workflow_symbol(db: Any, mapping: str, code_source: CodeSource) -> Any:
    module_path, symbol_name = mapping.rsplit(".", 1)
    checkout = await cached_repository_checkout(db, code_source)
    prepare_repository_import(checkout, module_path)
    module = importlib.import_module(module_path)
    module_file = getattr(module, "__file__", None)
    if module_file is None or not Path(module_file).resolve().is_relative_to(checkout):
        raise ImportError(f"'{mapping}' was not imported from pinned repository code")
    annotate_workflow_module(module, code_source)
    try:
        return getattr(module, symbol_name)
    except AttributeError as exc:
        raise LookupError(f"workflow symbol '{mapping}' was not found") from exc
