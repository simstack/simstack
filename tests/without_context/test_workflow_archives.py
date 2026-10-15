from __future__ import annotations

import io
import os
import stat
import struct
import zipfile

import pytest

from simstack.workflows.archive import (
    archive_digest,
    expanded_entry_count,
    extract_archive,
    snapshot_folder,
    validate_archive,
)


def _zip(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("simstack-workflow.toml", "[workflow]")
        for entry, value in entries:
            archive.writestr(entry, value)
    return output.getvalue()


def test_folder_snapshot_is_deterministic_and_extracts_exact_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "simstack-workflow.toml").write_text("[workflow]\n")
    (source / "code.py").write_text("print('do not execute during upload')\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "generated.pyc").write_bytes(b"cache")
    first = snapshot_folder(source)
    os.utime(source / "code.py", (0, 0))
    assert snapshot_folder(source) == first
    destination = tmp_path / "revision"
    assert extract_archive(first, destination) == archive_digest(first)
    assert (destination / "code.py").read_text() == (source / "code.py").read_text()
    assert not (destination / "__pycache__").exists()
    with pytest.raises(FileExistsError):
        extract_archive(first, destination)


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "C:/drive", "a\\b", "a/../b", "a//b", "./a"]
)
def test_archive_rejects_unsafe_paths_before_creating_destination(tmp_path, path):
    target = tmp_path / "new"
    with pytest.raises(ValueError):
        extract_archive(_zip([(path, b"x")]), target)
    assert not target.exists()


def test_archive_rejects_links_duplicates_and_file_parent_collision():
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    for entries in [
        [(link, b"target")],
        [("x", b"1"), ("x", b"2")],
        [("x", b"1"), ("x/y", b"2")],
    ]:
        with pytest.raises(ValueError):
            validate_archive(_zip(entries))


def test_archive_limits_are_operator_policy_and_manifest_is_required():
    data = _zip([("large", b"1234567890")])
    with pytest.raises(ValueError, match="unpacked"):
        validate_archive(data, max_unpacked_bytes=5)
    with pytest.raises(ValueError, match="entry count"):
        validate_archive(data, max_files=1)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("code.py", "x")
    with pytest.raises(ValueError, match="manifest|simstack-workflow"):
        validate_archive(output.getvalue())


def test_folder_symlink_is_not_followed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "simstack-workflow.toml").write_text("[workflow]\n")
    (source / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        snapshot_folder(source)


def test_file_backed_archive_has_identical_validation_digest_and_extraction(tmp_path):
    data = _zip([("code.py", b"never execute this code")])
    archive = tmp_path / "archive.zip"
    archive.write_bytes(data)
    assert validate_archive(archive) == validate_archive(data)
    assert archive_digest(archive) == archive_digest(data)
    destination = tmp_path / "source"
    assert extract_archive(archive, destination) == archive_digest(data)
    assert (destination / "code.py").read_bytes() == b"never execute this code"


def test_corrupt_payload_crc_is_a_validation_error_before_extraction(tmp_path):
    data = bytearray(_zip([("code.py", b"unique-payload")]))
    data[data.index(b"unique-payload")] ^= 1
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="corrupt|invalid ZIP"):
        extract_archive(bytes(data), destination)
    assert not destination.exists()


def test_corrupt_deflate_decoder_is_a_validation_error_before_extraction(tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("simstack-workflow.toml", "[workflow]")
        archive.writestr("code.py", b"compressible-content" * 10)
    data = bytearray(output.getvalue())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entry = archive.getinfo("code.py")
        offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
    data[offset] = 0x07  # Final DEFLATE block with the reserved/invalid block type.
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="corrupt|invalid ZIP"):
        extract_archive(bytes(data), destination)
    assert not destination.exists()


def test_browser_archive_manifest_marks_only_declared_executables(tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("simstack-workflow.toml", '[workflow]\nexecutables=["scripts/run.sh"]\n')
        archive.writestr("scripts/run.sh", "#!/bin/sh\necho done\n")
        archive.writestr("data.txt", "non-executable")
        native = zipfile.ZipInfo("scripts/native.sh")
        native.create_system = 3
        native.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(native, "#!/bin/sh\necho native\n")
    destination = tmp_path / "source"
    extract_archive(output.getvalue(), destination)
    assert os.access(destination / "scripts/run.sh", os.X_OK)
    assert os.access(destination / "scripts/native.sh", os.X_OK)
    assert not os.access(destination / "data.txt", os.X_OK)


@pytest.mark.parametrize("path", ["../outside", "/outside", "./scripts/run.sh", "scripts/../run.sh", "scripts\\run.sh", "scripts//run.sh", "missing.sh"])
def test_manifest_executables_cannot_escape_or_name_absent_files(tmp_path, path):
    import json

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("simstack-workflow.toml", '[workflow]\nexecutables=[' + json.dumps(path) + ']\n')
        archive.writestr("scripts/run.sh", "#!/bin/sh\n")
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="contained|regular file"):
        extract_archive(output.getvalue(), destination)
    assert not destination.exists()


@pytest.mark.parametrize("guard", ["compressed", "count"])
def test_archive_envelope_budgets_apply_before_zipfile_metadata_allocation(
    monkeypatch, guard
):
    data = _zip([("code.py", b"code")])
    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("ZipFile must not allocate rejected metadata")
    monkeypatch.setattr("simstack.workflows.archive.zipfile.ZipFile", forbidden_zipfile)
    options = {"max_archive_bytes": len(data) - 1} if guard == "compressed" else {"max_files": 1}
    with pytest.raises(ValueError, match="compressed size|entry count"):
        validate_archive(data, **options)


def test_entry_count_is_scanned_not_trusted_from_end_record(monkeypatch):
    data = bytearray(_zip([("one", b"1"), ("two", b"2")]))
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, end + 8, 1, 1)
    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("ZipFile must not allocate spoofed metadata")
    monkeypatch.setattr("simstack.workflows.archive.zipfile.ZipFile", forbidden_zipfile)
    with pytest.raises(ValueError, match="entry count"):
        validate_archive(bytes(data), max_files=2)


def _zip64_footer(data, *, eocd_count=None, locator_offset=None):
    """Add tiny ZIP64 records without allocating large files or entry lists."""
    end_offset = data.rfind(b"PK\x05\x06")
    eocd = bytearray(data[end_offset:])
    values = struct.unpack_from("<4s4H2LH", eocd)
    count, directory_size, directory_offset = values[4:7]
    zip64 = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0,
        count, count, directory_size, directory_offset,
    )
    locator = struct.pack(
        "<4sLQL", b"PK\x06\x07", 0,
        end_offset if locator_offset is None else locator_offset, 1,
    )
    if eocd_count is not None:
        struct.pack_into("<HH", eocd, 8, eocd_count, eocd_count)
    return data[:end_offset] + zip64 + locator + eocd


@pytest.mark.parametrize("locator_offset", [None, 0, 10_000_000])
def test_zip64_low_nonsentinel_eocd_count_cannot_bypass_preallocation_budget(
    monkeypatch, locator_offset
):
    data = _zip64_footer(
        _zip([("one", b"1"), ("two", b"2")]),
        eocd_count=1, locator_offset=locator_offset,
    )
    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("ZipFile must not allocate ZIP64 metadata over budget")
    monkeypatch.setattr("simstack.workflows.archive.zipfile.ZipFile", forbidden_zipfile)
    with pytest.raises(ValueError, match="entry count"):
        validate_archive(data, max_files=2)


@pytest.mark.parametrize("locator_offset", [None, 0, 10_000_000])
def test_small_zip64_uses_adjacent_footer_like_python_and_remains_supported(locator_offset):
    data = _zip64_footer(_zip([("code.py", b"code")]), locator_offset=locator_offset)
    assert [member.name for member in validate_archive(data)] == ["simstack-workflow.toml", "code.py"]


@pytest.mark.parametrize("zip64", [False, True])
def test_prepended_zip_data_uses_effective_directory_offset(zip64, tmp_path):
    data = _zip([("code.py", b"code")])
    if zip64:
        data = _zip64_footer(data)
    prefixed = b"small self-extractor stub\n" + data
    assert validate_archive(prefixed) == validate_archive(data)
    destination = tmp_path / "source"
    extract_archive(prefixed, destination)
    assert (destination / "code.py").read_bytes() == b"code"


def test_small_compressed_archive_cannot_expand_beyond_unpacked_budget(tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("simstack-workflow.toml", "[workflow]")
        archive.writestr("compressible", b"x" * 4096)
    data = output.getvalue()
    destination = tmp_path / "not-created"
    assert len(data) < 4096
    with pytest.raises(ValueError, match="unpacked size"):
        extract_archive(data, destination, max_unpacked_bytes=1024)
    assert not destination.exists()


@pytest.mark.parametrize("name", ["max_archive_bytes", "max_unpacked_bytes", "max_files", "max_path_bytes", "max_path_depth", "max_manifest_bytes"])
@pytest.mark.parametrize("value", [None, False, 0, -1, 1.5])
def test_archive_budgets_cannot_be_disabled_by_invalid_values(name, value):
    with pytest.raises(ValueError, match="positive integer"):
        validate_archive(_zip([]), **{name: value})


@pytest.mark.parametrize(
    ("path", "options", "error"),
    [
        ("a" * 31, {"max_path_bytes": 30}, "path length"),
        ("é" * 20, {"max_path_bytes": 30}, "path length"),
        ("a/b/c", {"max_path_depth": 2}, "path depth"),
    ],
)
def test_path_budgets_are_checked_before_constructing_and_expanding_paths(
    monkeypatch, tmp_path, path, options, error
):
    data = _zip([(path, b"data")])
    from simstack.workflows import archive as module
    original = module.PurePosixPath
    def guarded_path(value):
        if value == path:
            raise AssertionError("Rejected archive path must not be expanded")
        return original(value)
    monkeypatch.setattr(module, "PurePosixPath", guarded_path)
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match=error):
        extract_archive(data, destination, **options)
    assert not destination.exists()


@pytest.mark.parametrize("metadata", ["simstack-workflow.toml", "pyproject.toml"])
def test_manifest_and_pyproject_size_are_rejected_before_reading_or_extracting(
    monkeypatch, tmp_path, metadata
):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if metadata != "simstack-workflow.toml":
            archive.writestr("simstack-workflow.toml", "[workflow]")
        archive.writestr(metadata, b"#" * 33)
    def no_data_read(*args, **kwargs):
        raise AssertionError("Oversized metadata must be rejected before decompression/parser input")
    monkeypatch.setattr(zipfile.ZipFile, "open", no_data_read)
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="manifest size"):
        extract_archive(output.getvalue(), destination, max_manifest_bytes=32)
    assert not destination.exists()


def test_implicit_parent_directories_count_towards_entry_budget_before_writes(tmp_path):
    data = _zip([("one/two/three/code.py", b"code")])
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="entry count including parent"):
        extract_archive(data, destination, max_files=4)
    assert not destination.exists()
    assert expanded_entry_count(validate_archive(data, max_files=5)) == 5


@pytest.mark.parametrize("explicit_first", [False, True])
def test_shared_implicit_and_explicit_directories_are_counted_once_in_either_order(
    tmp_path, explicit_first
):
    files = [("shared/one.txt", b"1"), ("shared/two.txt", b"2")]
    entries = [("shared/", b""), *files] if explicit_first else [*files, ("shared/", b"")]
    data = _zip(entries)
    members = validate_archive(data, max_files=4)
    assert expanded_entry_count(members) == 4
    destination = tmp_path / "source"
    extract_archive(data, destination, max_files=4)
    assert (destination / "shared" / "one.txt").read_bytes() == b"1"
    assert (destination / "shared" / "two.txt").read_bytes() == b"2"


@pytest.mark.parametrize("parent_first", [False, True])
def test_parent_file_collision_is_rejected_in_either_archive_order(tmp_path, parent_first):
    entries = [("parent", b"file"), ("parent/child", b"child")]
    if not parent_first:
        entries.reverse()
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="file is also a parent"):
        extract_archive(_zip(entries), destination)
    assert not destination.exists()


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_unbounded_dictionary_compression_is_rejected_before_decoder_creation(monkeypatch, tmp_path, compression):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("simstack-workflow.toml", "[workflow]")
    def no_decoder(*args, **kwargs):
        raise AssertionError("Unsupported decoder must never be created")
    monkeypatch.setattr(zipfile.ZipFile, "open", no_decoder)
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="stored or deflated"):
        extract_archive(output.getvalue(), destination)
    assert not destination.exists()
