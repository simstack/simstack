"""Deterministic workflow snapshots and validated, non-executable ZIP extraction."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import struct
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from simstack.workflows.limits import DEFAULT_INSTALLATION_LIMITS


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    size: int
    directory: bool
    executable: bool


def _member_path(name: str, *, max_path_bytes: int, max_path_depth: int) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("Archive paths must be non-empty POSIX relative paths")
    if len(name.encode("utf-8")) > max_path_bytes:
        raise ValueError("Workflow archive exceeds configured path length")
    components = name.rstrip("/").split("/")
    if len(components) > max_path_depth:
        raise ValueError("Workflow archive exceeds configured path depth")
    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).drive:
        raise ValueError(f"Archive path must be relative: {name!r}")
    if any(part in ("", ".", "..") for part in components):
        raise ValueError(f"Archive path contains an unsafe component: {name!r}")
    return path


@contextmanager
def _archive_stream(data: bytes | Path):
    if isinstance(data, bytes):
        with io.BytesIO(data) as stream:
            yield stream
    else:
        with Path(data).open("rb") as stream:
            yield stream


def archive_digest(data: bytes | Path) -> str:
    digest = hashlib.sha256()
    with _archive_stream(data) as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _check_envelope(stream, *, max_archive_bytes: int, max_files: int) -> None:
    """Bound ZIP metadata allocation before ZipFile constructs its entry list."""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if size > max_archive_bytes:
        raise ValueError("Workflow archive exceeds configured compressed size")
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or len(tail) - position < 22:
        raise ValueError("Workflow archive has no valid ZIP directory")
    end = struct.unpack_from("<4s4H2LH", tail, position)
    if position + 22 + end[-1] != len(tail) or end[1] or end[2]:
        raise ValueError("Workflow archive has an invalid or multi-disk ZIP directory")
    count = end[4]
    directory_size, directory_offset = end[5], end[6]
    absolute = size - len(tail) + position
    directory_end = absolute
    locator = b""
    if absolute >= 20:
        stream.seek(absolute - 20)
        locator = stream.read(20)
    # ZipFile checks ZIP64 unconditionally, even without sentinel EOCD fields,
    # and reads the adjacent fixed record rather than the locator's offset.
    if locator[:4] == b"PK\x06\x07":
        _, disk, _offset, disks = struct.unpack("<4sLQL", locator)
        if disk or disks != 1 or absolute < 76:
            raise ValueError("Workflow ZIP64 directory is invalid")
        stream.seek(absolute - 76)
        record = stream.read(56)
        if len(record) != 56:
            raise ValueError("Workflow ZIP64 directory is incomplete")
        values = struct.unpack("<4sQ2H2L4Q", record)
        if values[0] != b"PK\x06\x06" or values[1] != 44 or values[4] or values[5] or values[6] != values[7]:
            raise ValueError("Workflow ZIP64 directory is invalid")
        count = values[7]
        directory_size, directory_offset = values[8], values[9]
        directory_end -= 76
    elif count == 65535 or directory_size == 0xffffffff or directory_offset == 0xffffffff:
        raise ValueError("Workflow ZIP64 directory is missing")
    if count > max_files:
        raise ValueError("Workflow archive exceeds configured entry count")
    # CPython adjusts offsets for prepended data. Bound precisely the directory
    # it will parse, not a decoy at the unadjusted EOCD offset.
    actual_offset = directory_end - directory_size
    if actual_offset < 0 or directory_offset > actual_offset:
        raise ValueError("Workflow ZIP directory is outside the archive")
    stream.seek(actual_offset)
    remaining, actual_count = directory_size, 0
    while remaining:
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise ValueError("Workflow ZIP directory entry is invalid")
        values = struct.unpack("<4s6H3L5H2L", header)
        length = 46 + values[10] + values[11] + values[12]
        if length > remaining:
            raise ValueError("Workflow ZIP directory entry is incomplete")
        actual_count += 1
        if actual_count > max_files:
            raise ValueError("Workflow archive exceeds configured entry count")
        stream.seek(length - 46, os.SEEK_CUR)
        remaining -= length
    if actual_count != count:
        raise ValueError("Workflow ZIP directory entry count is inconsistent")
    stream.seek(0)


def validate_archive(
    data: bytes | Path,
    *,
    max_archive_bytes: int = DEFAULT_INSTALLATION_LIMITS.max_archive_bytes,
    max_unpacked_bytes: int = DEFAULT_INSTALLATION_LIMITS.max_unpacked_bytes,
    max_files: int = DEFAULT_INSTALLATION_LIMITS.max_files,
    max_path_bytes: int = DEFAULT_INSTALLATION_LIMITS.max_path_bytes,
    max_path_depth: int = DEFAULT_INSTALLATION_LIMITS.max_path_depth,
    max_manifest_bytes: int = DEFAULT_INSTALLATION_LIMITS.max_manifest_bytes,
) -> tuple[ArchiveMember, ...]:
    """Validate every entry before any filesystem writes or Python imports."""
    for name, limit in (
        ("max_archive_bytes", max_archive_bytes),
        ("max_unpacked_bytes", max_unpacked_bytes),
        ("max_files", max_files),
        ("max_path_bytes", max_path_bytes),
        ("max_path_depth", max_path_depth),
        ("max_manifest_bytes", max_manifest_bytes),
    ):
        if type(limit) is not int or limit <= 0:
            raise ValueError(f"{name} must be a positive integer")
    with _archive_stream(data) as stream:
        try:
            _check_envelope(stream, max_archive_bytes=max_archive_bytes, max_files=max_files)
            return _validate_members(
                stream, max_unpacked_bytes=max_unpacked_bytes, max_files=max_files,
                max_path_bytes=max_path_bytes, max_path_depth=max_path_depth,
                max_manifest_bytes=max_manifest_bytes,
            )
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise ValueError("Workflow archive is corrupt or has invalid ZIP contents") from exc


def expanded_entry_count(members: tuple[ArchiveMember, ...]) -> int:
    """Count filesystem entries, including implicit parent directories, linearly."""
    tree = {}
    count = 0
    for member in members:
        node = tree
        for component in PurePosixPath(member.name).parts:
            if component not in node:
                node[component] = {}
                count += 1
            node = node[component]
    return count


def _validate_members(stream, *, max_unpacked_bytes, max_files, max_path_bytes, max_path_depth, max_manifest_bytes):
    members = []
    paths: dict[str, bool] = {}
    total_size = 0
    tree = {}
    expanded_count = 0
    with zipfile.ZipFile(stream) as archive:
        for entry in archive.infolist():
            path = _member_path(entry.filename, max_path_bytes=max_path_bytes, max_path_depth=max_path_depth)
            # Deflate/store have bounded decoder state. ZIP's LZMA header can
            # request a huge dictionary before the unpacked-byte cap is read.
            if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError("Workflow archives support stored or deflated ZIP entries")
            name = path.as_posix()
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if entry.flag_bits & 1:
                raise ValueError("Encrypted workflow archives are not supported")
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"Archive contains a link or special file: {name}")
            directory = entry.is_dir()
            if (kind == stat.S_IFDIR and not directory) or (
                kind == stat.S_IFREG and directory
            ):
                raise ValueError(f"Archive entry type disagrees with its path: {name}")
            if name in paths:
                raise ValueError(f"Archive contains duplicate paths: {name}")
            paths[name] = directory
            node = tree
            for component in path.parts:
                if node.get("file"):
                    raise ValueError("Archive file is also a parent directory")
                children = node.setdefault("children", {})
                if component not in children:
                    children[component] = {}
                    expanded_count += 1
                    if expanded_count > max_files:
                        raise ValueError("Workflow archive exceeds configured entry count including parent directories")
                node = children[component]
            if not directory:
                if node.get("children"):
                    raise ValueError("Archive file is also a parent directory")
                node["file"] = True
            if name in ("simstack-workflow.toml", "pyproject.toml") and entry.file_size > max_manifest_bytes:
                raise ValueError("Workflow metadata exceeds configured manifest size")
            total_size += entry.file_size
            if total_size > max_unpacked_bytes:
                raise ValueError("Workflow archive exceeds configured unpacked size")
            if len(paths) > max_files:
                raise ValueError("Workflow archive exceeds configured entry count")
            members.append(
                ArchiveMember(name, entry.file_size, directory, bool(mode & 0o111))
            )
        if paths.get("simstack-workflow.toml") is not False:
            raise ValueError(
                "Workflow archive must contain simstack-workflow.toml at its root"
            )
        # Reading checks CRC and actual decompressed contents before extraction.
        total_consumed = 0
        for entry in archive.infolist():
            with archive.open(entry) as stream:
                consumed = 0
                while block := stream.read(1024 * 1024):
                    consumed += len(block)
                    total_consumed += len(block)
                    if total_consumed > max_unpacked_bytes:
                        raise ValueError("Workflow archive exceeds configured unpacked size")
                    if consumed > entry.file_size:
                        raise ValueError("Workflow ZIP entry exceeds its declared size")
                if consumed != entry.file_size:
                    raise ValueError("Workflow ZIP entry has an inconsistent size")
    return tuple(members)


def snapshot_folder(source: str | os.PathLike[str]) -> bytes:
    """Package an exact folder snapshot independent of timestamps and ordering.

    Repository/tool caches are omitted; links and special files are rejected.
    The manifest remains mandatory and source files are never imported here.
    """
    root = Path(source).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workflow folder must be a directory")
    excluded = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
    }
    files = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directory = Path(current)
        directories[:] = sorted(name for name in directories if name not in excluded)
        for name in directories:
            if (directory / name).is_symlink():
                raise ValueError(
                    "Workflow snapshot must not contain directory symlinks"
                )
        for name in sorted(filenames):
            path = directory / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError(
                    f"Workflow snapshot contains a link or special file: {path.relative_to(root)}"
                )
            files.append(path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            entry = zipfile.ZipInfo(
                path.relative_to(root).as_posix(), date_time=(1980, 1, 1, 0, 0, 0)
            )
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | mode) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
    data = output.getvalue()
    validate_archive(data)
    return data


def extract_archive(data: bytes | Path, destination: str | os.PathLike[str], **limits) -> str:
    """Extract into a new directory; clean up only that new directory on failure."""
    members = validate_archive(data, **limits)
    root = Path(destination)
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        with _archive_stream(data) as stream, zipfile.ZipFile(stream) as archive:
            for member in members:
                target = root / member.name
                if member.directory:
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with (
                        archive.open(member.name) as source,
                        target.open("xb") as output,
                    ):
                        consumed = 0
                        while block := source.read(1024 * 1024):
                            consumed += len(block)
                            if consumed > member.size:
                                raise ValueError("Workflow ZIP entry changed during extraction")
                            output.write(block)
                        if consumed != member.size:
                            raise ValueError("Workflow ZIP entry changed during extraction")
                    target.chmod(0o700 if member.executable else 0o600)
        # Browser folder uploads cannot carry Unix permissions. The optional
        # manifest declaration restores only explicitly named regular files.
        from simstack.workflows.manifest import executable_paths

        for relative in executable_paths(root, max_bytes=limits.get("max_manifest_bytes", DEFAULT_INSTALLATION_LIMITS.max_manifest_bytes)):
            (root / relative).chmod(0o700)
    except BaseException:
        shutil.rmtree(root)
        raise
    return archive_digest(data)
