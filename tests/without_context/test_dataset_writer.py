"""Disk-backed dataset output publication never retains a membership cache."""
from __future__ import annotations

import asyncio
import copy
import errno
import gc
import hashlib
import json
import sqlite3
import tracemalloc
import warnings
import weakref
from pathlib import Path
from datetime import date, datetime, timezone

import pytest

from simstack.models import (
    BlobReference, DataSet, DataSetMetadata, DataSetSection, DataSetTuple, FileStack, IntData, StringData,
    dataset_write_scope,
)
from simstack.models.dataset_storage import (
    DatasetSectionHasher, _dataset_source_entry, dataset_io_scope,
    dataset_json_value, dataset_root_revision, dataset_section_revision, section_rows,
)
from simstack.models.dataset_writer import _RunnerWriteProvider
from simstack.models.document import EmbeddedModel, Model, Reference
from simstack.runner_api.datasets import DatasetRunnerClient, MAX_DATASET_PAGE_BYTES


class LinkedRow(Model):
    value: IntData = Reference()


class AttachedRow(Model):
    attachment: FileStack = Reference()


class StorageDetails(EmbeddedModel):
    blob: BlobReference
    raw: bytes
    created: datetime
    observed: date


class Provider:
    def __init__(self, root, *, retain=True):
        self.staging_dir = root
        self.retain = retain
        self.rows, self.headers, self.calls = {}, {}, []
        self.fail_section = None
        self.corrupt = None

    def publish_models(self, models):
        return [_dataset_source_entry(model) for model in models]

    def snapshot(self, dataset_id, section_key, *, kind, source, header):
        assert source.is_file()
        self.calls.append((section_key, source))
        if section_key == self.fail_section:
            raise OSError("snapshot failed")
        digest = DatasetSectionHasher(header["model_types"])
        count = 0
        rows = []
        with source.open("rb") as stream:
            for line in stream:
                row = json.loads(line)
                digest.update(row)
                assert row["position"] == count
                count += 1
                if self.retain:
                    rows.append(row)
        self.headers[section_key] = copy.deepcopy(header)
        if self.retain:
            self.rows[header["section_id"]] = rows
        reply = {"storage": {"version": 1, "dataset_id": str(dataset_id), "section_id": header["section_id"],
                 "revision": digest.hexdigest(), "row_count": count}, "column_defs": header["column_defs"]}
        if self.corrupt:
            reply["storage"][self.corrupt] = "f" * (64 if self.corrupt == "revision" else 24)
        return reply

    def page(self, storage, *, offset, limit, **kwargs):
        rows = self.rows[storage.section_id]
        stop = min(offset + limit, len(rows))
        return {"version": 1, "revision": storage.revision, "row_count": len(rows), "offset": offset,
                "rows": rows[offset:stop], "next_offset": stop if stop < len(rows) else None}

    def hydrate_documents(self, entries):
        return DatasetRunnerClient(None).hydrate_documents(entries)


@pytest.fixture(autouse=True)
def catalogue(monkeypatch):
    from simstack.core.runner_execution import hydration
    classes = {f"{cls.__module__}.{cls.__qualname__}": cls for cls in (IntData, StringData, LinkedRow, AttachedRow, FileStack)}
    monkeypatch.setattr(hydration, "_model_class", classes.__getitem__)


def dataset(kind="dict", **kwargs):
    cls = DataSet if kind == "dict" else DataSetTuple
    return cls(metadata=DataSetMetadata(field_name="writer"), **kwargs)


def assert_clean(provider):
    assert list(Path(provider.staging_dir).iterdir()) == []


@pytest.mark.parametrize("kind", ["dict", "tuple"])
def test_writer_publishes_compact_pages_names_types_order_and_graphs(tmp_path, kind):
    output, provider = dataset(kind), Provider(tmp_path)
    before = output.model_dump_doc()
    with dataset_write_scope(provider), output.writer() as writer:
        sink = writer.section("rows", model_types={"value": LinkedRow, "label": StringData} if kind == "dict" else [LinkedRow, StringData],
                              column_defs=[{"field": "value", "width": 123}])
        assert writer.section("rows") is sink
        for index in range(237):
            value, label = LinkedRow(value=IntData(value=index)), StringData(value=f"row-{index}")
            if kind == "dict":
                sink.add_row({"value": value, "label": label if index % 2 else None}, name=f"named-{index}")
            else:
                sink.append((value, label))
        writer.section("empty", model_types={} if kind == "dict" else [], column_defs=[{"field": "empty"}])
        assert sink.row_count == 237 and output.model_dump_doc() == before
    assert set(output.sections) == {"rows", "empty"}
    assert output.revision == dataset_root_revision(output.model_dump_doc())
    for section in output.sections.values():
        assert "_cache" not in section.__dict__
        assert section.inline_data == ({} if kind == "dict" else []) and section.table_entries == []
    assert output["rows"].storage.row_count == 237
    assert output["rows"].column_defs == [{"field": "value", "width": 123}]
    assert output["empty"].column_defs == [{"field": "empty"}]
    assert len(json.dumps(output.model_dump(), default=str)) < 4096
    with dataset_io_scope(provider):
        positions = []
        for page in output["rows"].iter_model_pages(limit=70):
            for row in page:
                positions.append(row.position)
                models = row.models
                value = models["value"] if kind == "dict" else models[0]
                assert value.value.value == row.position
                assert row.name == (f"named-{row.position}" if kind == "dict" else None)
                if kind == "dict":
                    assert ("label" in models) == bool(row.position % 2)
        assert positions == list(range(237))
    assert "_cache" not in output["rows"].__dict__
    assert_clean(provider)
    with pytest.raises(RuntimeError, match="closed"):
        sink.add_row({}) if kind == "dict" else sink.append(())


def test_first_nonempty_inference_spools_empty_rows_and_sparse_hash_matches_canonical(tmp_path):
    output, provider = dataset(), Provider(tmp_path)
    with dataset_write_scope(provider), output.writer() as writer:
        sink = writer.section("rows")
        sink.add_row({}, name="empty-first")
        sink.add_row({"ignored": None}, name="empty-second")
        sink.add_row({"value": IntData(value=2**60)}, name="filled")
        sink.add_row({}, name="empty-last")
    rows = provider.rows[output["rows"].storage.section_id]
    assert output["rows"].model_types == {"value": "IntData"}
    assert rows[0]["entries"] == rows[1]["entries"] == rows[3]["entries"] == [{}]
    assert output["rows"].storage.revision == dataset_section_revision({"value": "IntData"}, rows)
    assert_clean(provider)


def test_row_is_frozen_at_add_and_duplicate_root_slots_are_deduplicated(tmp_path):
    output, provider = dataset(), Provider(tmp_path)
    value = IntData(value=1)
    with dataset_write_scope(provider), output.writer() as writer:
        sink = writer.section("rows")
        sink.add_row({"first": value, "second": value})
        value.value = 99
    rows = provider.rows[output["rows"].storage.section_id]
    assert len(rows[0]["documents"]) == 1
    assert rows[0]["documents"][0]["value"]["value"] == 1
    assert len(rows[0]["refs"]) == 2
    assert output["rows"].storage.revision == dataset_section_revision(output["rows"].model_types, rows)


@pytest.mark.parametrize("cause", ["name", "row_id", "type", "slot", "model", "metadata", "identity"])
def test_validation_failure_poisoned_context_cleans_and_never_attaches(tmp_path, cause):
    output, provider = dataset(), Provider(tmp_path)
    with dataset_write_scope(provider):
        with pytest.raises((ValueError, TypeError)):
            with output.writer() as writer:
                sink = writer.section("rows", model_types={"value": IntData})
                sink.add_row({"value": IntData(value=1)}, name="one", row_id="1" * 24)
                if cause == "metadata":
                    writer.section("rows", model_types={"other": StringData})
                else:
                    models = {"value": IntData(value=2)}
                    if cause == "type":
                        models["value"] = StringData(value="wrong")
                    elif cause == "slot":
                        models["other"] = IntData(value=2)
                    elif cause == "model":
                        models["value"] = b"undeclared bytes"
                    sink.add_row(models, name="one" if cause == "name" else "two",
                                 row_id="1" * 24 if cause == "row_id" else "invalid" if cause == "identity" else None)
        assert output.sections == {} and output.revision is None
        assert_clean(provider)
    assert provider.calls == []


def test_caught_row_error_cannot_commit_partial_output(tmp_path):
    output, provider = dataset(), Provider(tmp_path)
    with dataset_write_scope(provider), pytest.raises(RuntimeError, match="failed"):
        with output.writer() as writer:
            sink = writer.section("rows")
            sink.add_row({"value": IntData(value=1)}, "same")
            with pytest.raises(ValueError, match="duplicate"):
                sink.add_row({"value": IntData(value=2)}, "same")
    assert output.sections == {} and provider.calls == []
    assert_clean(provider)


@pytest.mark.parametrize("kind", ["dict", "tuple"])
def test_empty_sections_and_stable_ids_across_exact_retries(tmp_path, kind):
    snapshots = []
    for _ in range(2):
        output, provider = dataset(kind, id="1" * 24), Provider(tmp_path)
        with dataset_write_scope(provider), output.writer() as writer:
            sink = writer.section("rows")
            for index in range(3):
                model = IntData(id=f"{index + 2:024x}", value=index)
                sink.add_row({"value": model}, name=str(index)) if kind == "dict" else sink.append((model,))
            writer.section("empty")
        snapshots.append(output.model_dump_doc())
        assert output["empty"].storage.row_count == 0
        assert_clean(provider)
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("failure", ["network", "cancel", "revision", "dataset_id", "section_id"])
def test_mid_close_failure_never_attaches_preceding_successful_section(tmp_path, failure):
    output, provider = dataset(), Provider(tmp_path)
    if failure == "network":
        provider.fail_section = "second"
    elif failure not in {"cancel"}:
        provider.corrupt = failure
    expected = asyncio.CancelledError if failure == "cancel" else (OSError, ValueError)
    with dataset_write_scope(provider), pytest.raises(expected):
        with output.writer() as writer:
            writer.section("first").add_row({"value": IntData(value=1)})
            writer.section("second").add_row({"value": IntData(value=2)})
            if failure == "cancel":
                raise asyncio.CancelledError()
    assert output.sections == {} and output.revision is None
    assert_clean(provider)


def test_budget_and_conflicting_same_id_fail_without_inline_fallback(tmp_path):
    for models, match in [({"value": StringData(value="x" * MAX_DATASET_PAGE_BYTES)}, "byte budget"),
                          ({"first": IntData(id="1" * 24, value=1), "second": IntData(id="1" * 24, value=2)}, "conflicting")]:
        output, provider = dataset(), Provider(tmp_path)
        with dataset_write_scope(provider), pytest.raises(ValueError, match=match):
            with output.writer() as writer:
                writer.section("rows").add_row(models)
        assert output.sections == {} and provider.calls == []
        assert_clean(provider)


def test_existing_root_and_mid_write_root_mutation_are_protected(tmp_path):
    provider = Provider(tmp_path)
    for output in (dataset(revision="1" * 64), dataset(sections={"old": DataSetSection()})):
        before = output.model_dump_doc()
        with dataset_write_scope(provider), pytest.raises(ValueError, match="new root"):
            with output.writer():
                pass
        assert output.model_dump_doc() == before
    output = dataset()
    with dataset_write_scope(provider), pytest.raises(ValueError, match="new root"):
        with output.writer() as writer:
            writer.section("rows").add_row({"value": IntData(value=1)})
            output.sections["external"] = DataSetSection()
    assert list(output.sections) == ["external"] and provider.calls == []
    assert_clean(provider)


@pytest.mark.parametrize("operation", ["write", "flush", "close"])
def test_disk_errors_preserve_original_and_cleanup_all_spools_and_database(tmp_path, operation):
    output, provider = dataset(), Provider(tmp_path)
    original = OSError(errno.ENOSPC, f"original {operation} failure")
    secondary = OSError(errno.EIO, "secondary cleanup failure")
    handles = []

    class FaultyFile:
        def __init__(self, target, *, primary=False):
            self.target, self.primary = target, primary

        def __getattr__(self, name):
            return getattr(self.target, name)

        def write(self, data):
            if self.primary and operation == "write":
                raise original
            return self.target.write(data)

        def flush(self):
            if self.primary and operation == "flush":
                raise original
            return self.target.flush()

        def close(self):
            self.target.close()
            raise original if self.primary and operation == "close" else secondary

    class FaultyDatabase:
        def __init__(self, target):
            self.target = target

        def __getattr__(self, name):
            return getattr(self.target, name)

        def close(self):
            self.target.close()
            raise secondary

    with dataset_write_scope(provider), pytest.raises(OSError) as caught:
        with output.writer() as writer:
            first, second, third = (writer.section(name, model_types={"value": IntData})
                                    for name in ("first", "second", "third"))
            handles = [section._file for section in (first, second, third)]
            database = writer._database
            writer._database = FaultyDatabase(database)
            first._file = FaultyFile(first._file, primary=True)
            second._file = FaultyFile(second._file)
            first.add_row({"value": IntData(value=1)})
    assert caught.value is original
    assert all(handle.closed for handle in handles)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")
    assert output.sections == {} and output.revision is None
    assert provider.calls == []
    assert_clean(provider)


def test_cleanup_database_error_after_staging_does_not_attach_root(tmp_path):
    output, provider = dataset(), Provider(tmp_path)
    failure = OSError(errno.EIO, "database cleanup failed")

    class FaultyDatabase:
        def __init__(self, target):
            self.target = target

        def __getattr__(self, name):
            return getattr(self.target, name)

        def close(self):
            self.target.close()
            raise failure

    with dataset_write_scope(provider), pytest.raises(OSError) as caught:
        with output.writer() as writer:
            writer.section("rows").add_row({"value": IntData(value=1)})
            writer._database = FaultyDatabase(writer._database)
    assert caught.value is failure
    assert len(provider.calls) == 1  # Snapshot staged; root is still unpublished.
    assert output.sections == {} and output.revision is None
    assert_clean(provider)


def test_default_requires_capability_claim_and_explicit_scope_never_runs_inline(tmp_path):
    output = dataset()
    with pytest.raises(RuntimeError, match="dataset_storage"):
        with output.writer():
            pass
    assert output.sections == {}
    assert_clean(Provider(tmp_path))


def test_runner_provider_publishes_blobs_and_nested_datasets_before_graph_serialization(monkeypatch):
    from simstack.core.runner_execution import blobs, serialization
    calls = []
    model = IntData(value=1)
    monkeypatch.setattr(blobs, "publish_runtime_blobs", lambda values: calls.append(("publish", values[0].id)))

    def entry(value):
        assert calls == [("publish", value.id)]
        calls.append(("encode", value.id))
        return _dataset_source_entry(value)

    monkeypatch.setattr(serialization, "_typed_document_entry", entry)
    assert _RunnerWriteProvider.__new__(_RunnerWriteProvider).publish_models([model])[0]["value"]["value"] == 1
    assert [item[0] for item in calls] == ["publish", "encode"]


def test_embedded_projection_descriptors_keep_rich_values_and_reject_unknown_objects():
    descriptor = StorageDetails(blob=BlobReference(id="1" * 24, size_bytes=3, checksum_sha256="a" * 64),
        raw=b"\x00\xff", created=datetime(2026, 10, 19, 12, 34, 56, tzinfo=timezone.utc), observed=date(2026, 10, 19))
    encoded = dataset_json_value({"storage": descriptor})
    assert encoded == {"storage": {"blob": {"id": "1" * 24, "size_bytes": 3, "checksum_sha256": "a" * 64},
        "raw": {"$binary": {"base64": "AP8=", "subType": "00"}},
        "created": {"$date": "2026-10-19T12:34:56+00:00"}, "observed": {"$date": "2026-10-19"}}}
    assert dataset_json_value(encoded) == encoded
    with pytest.raises(ValueError, match="Unsupported dataset projection value object"):
        dataset_json_value({"unsupported": object()})


def test_blob_reference_projection_works_for_writer_and_ordinary_section_rows(tmp_path, monkeypatch):
    from simstack.core.runner_execution import blobs, serialization

    payload = b"attachment bytes"
    attachment = FileStack.from_bytes(payload, "original.txt")
    original = AttachedRow(attachment=attachment)
    checksum = hashlib.sha256(payload).hexdigest()

    def publish(models):
        for row in models:
            row.attachment.blob = BlobReference(id=str(row.attachment.id), size_bytes=len(payload), checksum_sha256=checksum)
            row.attachment.in_memory = False
            row.attachment.content = None
            row.attachment.locations = []

    monkeypatch.setattr(blobs, "publish_runtime_blobs", publish)
    monkeypatch.setattr(serialization, "_typed_document_entry", _dataset_source_entry)
    output, provider = dataset(), Provider(tmp_path)
    provider.publish_models = _RunnerWriteProvider.__new__(_RunnerWriteProvider).publish_models
    with dataset_write_scope(provider), output.writer() as writer:
        writer.section("files").add_row({"row": original}, name="file-row")
    rows = provider.rows[output["files"].storage.section_id]
    blob = {"id": str(attachment.id), "size_bytes": len(payload), "checksum_sha256": checksum}
    assert rows[0]["entries"][0]["attachment"]["blob"] == blob
    section = DataSetSection()
    section.add_row({"row": original}, name="file-row")
    ordinary = next(section_rows(section))
    assert dataset_json_value(ordinary)["entries"] == rows[0]["entries"]
    assert ordinary["refs"][0]["graph_hash"] == rows[0]["refs"][0]["graph_hash"]
    attachment.name = "changed-after-snapshot.txt"
    attachment.blob = BlobReference(id=str(attachment.id), size_bytes=999, checksum_sha256="b" * 64)
    with dataset_io_scope(provider):
        restored = next(output["files"].iter_models()).models["row"].attachment
    assert restored.name == "original.txt" and restored.blob.model_dump() == blob
    assert "_cache" not in output["files"].__dict__
    assert_clean(provider)


def test_large_generator_uses_disk_uniqueness_and_releases_every_model(tmp_path):
    output, provider = dataset(), Provider(tmp_path, retain=False)
    # No recording provider lists or weakref arrays scale with row count.
    tracemalloc.start()
    try:
        with warnings.catch_warnings(), dataset_write_scope(provider), output.writer() as writer:
            warnings.simplefilter("ignore", DeprecationWarning)
            sink = writer.section("rows", model_types={"value": IntData})
            for index in range(6000):
                value = IntData(value=index)
                reference = weakref.ref(value)
                sink.add_row({"value": value}, name=f"row-{index}")
                del value
                assert reference() is None
            gc.collect()
            _, peak = tracemalloc.get_traced_memory()
            assert peak < 6 * 1024 * 1024
            assert sink.row_count == 6000
    finally:
        tracemalloc.stop()
    assert output["rows"].storage.row_count == 6000
    assert "_cache" not in output["rows"].__dict__
    assert_clean(provider)
