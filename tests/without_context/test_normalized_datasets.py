"""Normalized datasets remain compact and preserve Python membership semantics."""
from __future__ import annotations

import pytest

from simstack.core.node import compute_arg_hash
from simstack.models import IntData, DataSet, DataSetSection, DataSetTupleSection, DataSetMetadata
from simstack.core.hash import complex_hash_function
from simstack.models.dataset import DataSetSelection, DataSetSelectionField
from simstack.models.dataset_storage import (SharedDatasetSectionStorage, dataset_io_scope,
    dataset_section_revision, section_rows, section_row_ids, dataset_projection_cells, dataset_source_hash,
    dataset_json_value)


class Provider:
    def __init__(self, rows, models, revision):
        self.rows, self.models, self.revision, self.requests = rows, models, revision, []
    def page(self, storage, *, kind, section_key, offset, limit):
        self.requests.append((kind, section_key, offset, limit))
        rows = self.rows[offset:offset + limit]
        return {"version": 1, "revision": self.revision, "row_count": len(self.rows), "offset": offset,
                "rows": rows, "next_offset": offset + len(rows) if offset + len(rows) < len(self.rows) else None}
    def hydrate_documents(self, entries):
        return self.models


def normalized(section, dataset_id=None):
    rows = list(section_rows(section))
    revision = dataset_section_revision(section.model_types, rows)
    models = {str(model.id): model for row in section._get_cache().values() for model in row.values()} if isinstance(section, DataSetSection) else {str(model.id): model for row in section._get_cache() for model in row}
    doc = section.model_dump()
    doc.update(data={} if isinstance(section, DataSetSection) else [], table_entries=[],
        storage={"dataset_id": str(dataset_id or "0" * 24), "section_id": "a" * 24, "revision": revision,
                 "row_count": len(rows)})
    return type(section).model_validate(doc), Provider(rows, models, revision)


def test_named_section_lazy_pages_full_python_access_and_compact_serialization():
    source = DataSetSection()
    for index in range(205):
        source.add_row({"value": IntData(value=index)}, name=f"row-{index}")
    target, provider = normalized(source)
    assert len(target) == 205 and provider.requests == []
    assert target.model_dump()["data"] == {}
    with dataset_io_scope(provider):
        assert target["row-204"]["value"].value == 204
        assert len(provider.requests) == 3
        assert len(list(target.items())) == 205
    assert target.model_dump()["data"] == {}
    assert target.data["row-0"]["value"] == source["row-0"]["value"].id
    assert complex_hash_function(source) == complex_hash_function(target)
    previous = section_row_ids(target)["row-0"]
    row = target.pop("row-0")
    target["renamed"] = row
    assert section_row_ids(target)["renamed"] == previous
    assert complex_hash_function(source) != complex_hash_function(target)


@pytest.mark.asyncio
async def test_tuple_duplicate_rows_keep_distinct_ids_and_mutations_preserve_identity():
    section = DataSetTupleSection()
    value = IntData(value=1)
    await section.append((value,))
    await section.append((value,))
    original = list(section_row_ids(section))
    assert original[0] != original[1]
    target, provider = normalized(section)
    assert len(target) == 2 and provider.requests == []
    with dataset_io_scope(provider):
        assert (await target.get_model_group(1))[0].value == 1
    target.reverse()
    assert section_row_ids(target) == original[::-1]
    await target.insert(1, (IntData(value=2),))
    assert section_row_ids(target)[0] == original[1]
    assert section_row_ids(target)[2] == original[0]
    await target.pop(1)
    assert section_row_ids(target) == original[::-1]
    assert target.model_dump()["data"] == []


def test_normalized_section_without_scoped_access_is_not_silently_empty():
    section = DataSetSection(model_types={"value": "IntData"}, storage=SharedDatasetSectionStorage(
        dataset_id="0" * 24, section_id="a" * 24, revision="b" * 64, row_count=1))
    assert len(section) == 1
    with pytest.raises(RuntimeError, match="scoped dataset provider"):
        section.values()


def test_pinned_pages_reject_revision_drift_gaps_and_duplicate_ids():
    source = DataSetSection()
    source.add_row({"value": IntData(value=1)}, "one")
    target, provider = normalized(source)
    provider.revision = "c" * 64
    with dataset_io_scope(provider), pytest.raises(ValueError, match="pinned snapshot"):
        list(target.iter_row_pages())
    provider.revision = target.storage.revision
    provider.rows[0]["position"] = 10
    with dataset_io_scope(provider), pytest.raises(ValueError, match="gap"):
        target.values()


def test_dataset_root_hash_ignores_snapshot_physical_fields_and_normalization():
    original = DataSet(metadata=DataSetMetadata(field_name="test"))
    original["rows"].add_row({"value": IntData(value=1)}, "one")
    before = compute_arg_hash([original])
    section, provider = normalized(original["rows"], original.id)
    restored = DataSet(id=original.id, metadata=original.metadata, revision="c" * 64, sections={"rows": section})
    assert compute_arg_hash([restored]) == before and provider.requests == []
    with dataset_io_scope(provider):
        restored["rows"]["one"]["value"].value = 2
    assert compute_arg_hash([restored]) != before


def test_selection_legacy_indices_are_never_reinterpreted_for_normalized_sources():
    value = DataSetSelection(dataset_id="0" * 24, dataset_selection_fields=[DataSetSelectionField(indices=[1])])
    assert value.dataset_selection_fields[0].indices == [1]
    with pytest.raises(ValueError, match="stable row IDs"):
        DataSetSelection(dataset_id="0" * 24, source_revision="a" * 64,
            dataset_selection_fields=[DataSetSelectionField(indices=[1])])


def test_projection_keys_are_rfc6901_and_revision_includes_values():
    cells = dataset_projection_cells([{"a/b": b"bytes", "big": 2**63}, {"x~y": float("nan")}], {"slot/one": "A", "slot~two": "B"})
    assert cells["/slot~1one/a~1b"]["$binary"]["base64"] == "Ynl0ZXM="
    assert cells["/slot~1one/big"] == {"$numberDecimal": str(2**63)}
    assert cells["/slot~0two/x~0y"] == {"$numberDouble": "NaN"}
    row = {"row_id": "a" * 24, "position": 0, "row_name": "x", "refs": [], "entries": [{"value": 1}]}
    old = dataset_section_revision({}, [row])
    row["entries"][0]["value"] = 2
    assert dataset_section_revision({}, [row]) != old
    wire = dataset_json_value(row)
    assert dataset_section_revision({}, [wire]) == dataset_section_revision({}, [row])
    assert dataset_source_hash({"_id": "0" * 24, "value": b"x"}) == dataset_source_hash({"id": "0" * 24, "value": {"$binary": {"base64": "eA==", "subType": "00"}}})


def test_source_hash_matches_mongo_datetime_storage_precision():
    from datetime import datetime, date, timezone, timedelta
    original = {"id": "0" * 24, "when": datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=2))), "day": date(2026, 1, 2)}
    stored = {"_id": "0" * 24, "when": datetime(2026, 1, 1, 10, 0, 0, 123000), "day": "2026-01-02"}
    assert dataset_source_hash(original) == dataset_source_hash(stored)


def test_explicit_numeric_envelopes_are_lossless_and_shape_checked():
    import math
    from decimal import Decimal
    from simstack.models.document import decode_document_value, DocumentParsingError
    assert decode_document_value({"$numberLong": str(2**63 - 1)}) == 2**63 - 1
    assert math.isnan(decode_document_value({"$numberDouble": "NaN"}))
    assert decode_document_value({"$numberDecimal": "1.25"}) == Decimal("1.25")
    for value in [{"$numberLong": "01"}, {"$numberLong": str(2**63)}, {"$numberLong": 1},
                  {"$numberDouble": "nan"}, {"$numberDecimal": "Infinity"},
                  {"$numberLong": "1", "extra": 2}]:
        with pytest.raises(DocumentParsingError):
            decode_document_value(value)


def test_dataset_snapshot_staging_streams_rows_before_compact_root_and_reuses_unchanged(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from simstack.core.runner_execution import serialization, datasets, blobs
    from simstack.runner_api.datasets import DatasetRunnerClient
    fake_context = SimpleNamespace(initialized=True, runner_mode=True,
        config=SimpleNamespace(object_storage=True, dataset_storage=True, workdir=tmp_path))
    monkeypatch.setattr("simstack.core.context.context", fake_context)
    monkeypatch.setattr(serialization, "_mapping_for_model", lambda model: SimpleNamespace(
        mapping=f"{type(model).__module__}.{type(model).__qualname__}"))
    published, snapshots = [], []
    monkeypatch.setattr(blobs, "publish_runtime_blobs", lambda values: published.extend(values))
    class Client:
        def snapshot(self, dataset_id, section_key, *, kind, source):
            assert published
            with source.open() as stream:
                header = json.loads(next(stream))
                rows = [json.loads(line) for line in stream]
            snapshots.append((header, rows))
            return {"storage": {"dataset_id": str(dataset_id), "section_id": header["section_id"],
                "revision": dataset_section_revision(header["model_types"], rows), "row_count": len(rows)}, "column_defs": []}
    monkeypatch.setattr(DatasetRunnerClient, "from_context", classmethod(lambda cls: Client()))
    value = DataSet(metadata=DataSetMetadata(field_name="test"))
    for i in range(120):
        value["rows"].add_row({"value": IntData(value=i)}, name=str(i))
    previous_hash = compute_arg_hash([value])
    datasets.publish_runtime_datasets([value])
    assert len(snapshots) == 1 and len(snapshots[0][1]) == 120
    assert value.model_dump_doc()["sections"]["rows"]["data"] == {}
    assert value.model_dump_doc()["sections"]["rows"]["table_entries"] == []
    assert compute_arg_hash([value]) == previous_hash
    assert snapshots[0][1][0]["documents"][0]["value"]["id"]
    datasets.publish_runtime_datasets([value])
    assert len(snapshots) == 1
    compact = DataSet.model_validate_doc(value.model_dump_doc())
    datasets.publish_runtime_datasets([compact])
    assert len(snapshots) == 1  # No page loader or row-model reads are needed.


def test_source_membership_hash_keeps_original_projection_when_sources_have_not_changed():
    section = DataSetSection()
    section.add_row({"value": IntData(value=1)}, "row")
    target, provider = normalized(section)
    provider.rows[0]["entries"] = [{"old_renderer_label": "one"}]
    target.storage.revision = provider.revision = dataset_section_revision(section.model_types, provider.rows)
    original = target.storage.revision
    with dataset_io_scope(provider):
        assert target["row"]["value"].value == 1
    assert dataset_section_revision(target.model_types, section_rows(target)) == original
    target["row"]["value"].value = 2
    assert dataset_section_revision(target.model_types, section_rows(target)) != original


@pytest.mark.asyncio
async def test_tuple_pop_and_insert_move_keeps_the_existing_row_identity():
    section = DataSetTupleSection()
    await section.append((IntData(value=1),))
    await section.append((IntData(value=2),))
    previous = list(section_row_ids(section))
    moved = await section.pop(0)
    await section.insert(1, moved)
    assert section_row_ids(section) == previous[::-1]


def test_dataset_http_upload_stream_retries_and_keeps_claim_path_and_token(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit
    from simstack.runner_api.datasets import DatasetRunnerClient
    from simstack.runner_protocol import dataset_section_path
    path = dataset_section_path("task", "dataset", "section/with space", action="snapshot",
        kind="dict", claim_id="claim", attempt=2)
    parsed = urlsplit(path)
    assert "section%2Fwith%20space/snapshot" in parsed.path
    assert parse_qs(parsed.query) == {"claim_id": ["claim"], "attempt": ["2"], "kind": ["dict"]}
    calls, closes = [], []
    class Connection:
        def request(self, method, path, body=None, headers=None):
            assert hasattr(body, "read")
            calls.append((method, path, body.read(), headers))
        def getresponse(self):
            raw = b'{"detail":"retry"}' if len(calls) == 1 else b'{"storage":{}}'
            return SimpleNamespace(status=503 if len(calls) == 1 else 200, read=lambda size: raw)
        def close(self):
            closes.append(True)
    transport = SimpleNamespace(connection=Connection, path=lambda path: "/api/runner" + path)
    api = SimpleNamespace(_transport=transport, runner_token="execution-only", retry_attempts=1, retry_backoff_seconds=0)
    client = DatasetRunnerClient(api)
    source = tmp_path / "snapshot.ndjson"
    source.write_bytes(b'{"version":1}\n{"row_id":"x"}\n')
    reply = client._request("PUT", path, source)
    assert reply == {"storage": {}} and len(calls) == len(closes) == 2
    assert calls[0][2] == calls[1][2] == source.read_bytes()
    assert calls[0][3]["Authorization"] == "Bearer execution-only"
    assert calls[0][3]["Content-Type"] == "application/x-ndjson"
    assert int(calls[0][3]["Content-Length"]) == source.stat().st_size


@pytest.mark.parametrize("status,raw,match", [(409, b'{"detail":"stale"}', "HTTP 409"),
    (200, b'invalid', "invalid JSON"), (200, b'[]', "non-object"),
    (200, b'x' * (4 * 1024 * 1024 + 1), "page budget")],
    ids=["stale", "invalid-json", "non-object", "too-large"])
def test_dataset_http_failures_are_bounded_and_do_not_retry_stale_claim(status, raw, match):
    from types import SimpleNamespace
    from simstack.runner_api.datasets import DatasetRunnerClient
    from simstack.runner_api.errors import RunnerApiError
    calls = []
    class Connection:
        def request(self, *args, **kwargs):
            calls.append(True)
        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda size: raw[:size])
        def close(self):
            pass
    api = SimpleNamespace(_transport=SimpleNamespace(connection=Connection, path=lambda path: path),
        runner_token="execution-only", retry_attempts=2, retry_backoff_seconds=0)
    with pytest.raises(RunnerApiError, match=match):
        DatasetRunnerClient(api)._request("GET", "/rows")
    assert len(calls) == 1


def test_dataset_and_selection_schemas_declare_inherited_kinds():
    from simstack.models.datasettuple import DataSetTuple, DataSetTupleSelection
    class ChildDataSet(DataSet):
        pass
    for cls, marker, kind in [(ChildDataSet, "x-simstack-dataset", "dict"),
        (DataSetTuple, "x-simstack-dataset", "tuple"),
        (DataSetSelection, "x-simstack-dataset-selection", "dict"),
        (DataSetTupleSelection, "x-simstack-dataset-selection", "tuple")]:
        assert cls.model_json_schema()[marker] == {"version": 1, "kind": kind}
        assert cls.ui_schema()["ui:options"]["datasetKind"] == kind


def test_compact_dataset_root_revision_ignores_projection_locator_and_wire_identity():
    import copy
    from simstack.models.dataset_storage import dataset_root_revision
    value = {"_id": "1" * 24, "revision": "a" * 64, "metadata": {"label": "дата"},
        "sections": {"rows": {"storage": {"dataset_id": "1" * 24, "section_id": "2" * 24,
            "revision": "b" * 64, "row_count": 1,
            "projection": {"file": "3" * 24}}, "data": {}, "table_entries": []}}}
    before = dataset_root_revision(value)
    after = copy.deepcopy(value)
    after["id"] = after.pop("_id")
    after["revision"] = "c" * 64
    after["sections"]["rows"]["storage"]["projection"] = {"file": "4" * 24, "codec_version": 2}
    assert dataset_root_revision(after) == before
    assert value["sections"]["rows"]["storage"]["projection"]["file"] == "3" * 24
    after["metadata"]["label"] = "changed"
    assert dataset_root_revision(after) != before


def test_prepared_dataset_provider_confines_paths_and_slices_bounded_pages(tmp_path):
    import json
    from simstack.workflows.dataset_io import PreparedDatasetIO
    storage = SharedDatasetSectionStorage(dataset_id="1" * 24, section_id="2" * 24,
        revision="a" * 64, row_count=3)
    page = {"version": 1, "revision": storage.revision, "offset": 0, "row_count": 3,
        "rows": [{"position": index} for index in range(3)], "next_offset": None}
    source = tmp_path / "page.json"
    source.write_text(json.dumps(page))
    section = {"dataset_id": str(storage.dataset_id), "section_id": storage.section_id,
        "revision": storage.revision, "kind": "dict", "section_key": "rows",
        "pages": [{"offset": 0, "row_count": 3, "path": "page.json"}]}
    provider = PreparedDatasetIO({"files_root": str(tmp_path), "dataset_io": {"sections": [section]}})
    result = provider.page(storage, kind="dict", section_key="rows", offset=1, limit=1)
    assert result["rows"] == [{"position": 1}] and result["next_offset"] == 2
    section["pages"][0]["path"] = "../outside.json"
    with pytest.raises(ValueError, match="confined"):
        provider.page(storage, kind="dict", section_key="rows", offset=1, limit=1)
    with pytest.raises(ValueError, match="Duplicate"):
        PreparedDatasetIO({"files_root": str(tmp_path), "dataset_io": {"sections": [section, section]}})


def test_membership_publication_is_independent_of_blob_storage_capability(monkeypatch):
    from types import SimpleNamespace
    from simstack.core.runner_execution import blobs, datasets
    publications = []
    monkeypatch.setattr("simstack.core.runner_execution.blobs.context", SimpleNamespace(
        initialized=True, config=SimpleNamespace(object_storage=False, dataset_storage=True)))
    monkeypatch.setattr(datasets, "publish_runtime_datasets", lambda roots: publications.append(roots))
    blobs.publish_runtime_blobs(["dataset"])
    assert publications == [["dataset"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dict", "tuple"])
async def test_http_dataset_python_access_hydrates_actual_server_write_shaped_row_bundles(monkeypatch, kind):
    from simstack.models import StringData
    from simstack.runner_api.datasets import DatasetRunnerClient
    from simstack.core.runner_execution import hydration
    from simstack.core.runner_execution.serialization import _model_document
    number, text = IntData(value=17), StringData(value="seventeen")
    if kind == "dict":
        source = DataSetSection()
        source.add_row({"value":number,"label":text},"one")
    else:
        source = DataSetTupleSection()
        await source.append((number,text))
    target, response = normalized(source)
    response.rows[0]["documents"] = [{"variable_mapping":f"{type(model).__module__}.{type(model).__qualname__}",
        "value":_model_document(model),"dependencies":[]} for model in (number,text)]
    assert all("reference" not in entry for entry in response.rows[0]["documents"])
    catalogue = {f"{cls.__module__}.{cls.__qualname__}":cls for cls in (IntData,StringData)}
    monkeypatch.setattr(hydration,"_model_class",lambda mapping:catalogue[mapping])
    client = DatasetRunnerClient(None)
    monkeypatch.setattr(client,"page",response.page)
    with dataset_io_scope(client):
        if kind == "dict":
            row = target["one"]
            assert row["value"].value == 17 and row["label"].value == "seventeen"
            assert row["value"] is not number
        else:
            row = await target.get_model_group(0)
            assert row[0].value == 17 and row[1].value == "seventeen" and row[0] is not number
            assert await target.get_all_model_groups() == [row]
    assert target.model_dump()["data"] == ({} if kind == "dict" else [])


def test_dataset_row_document_converter_accepts_canonical_storage_alias_without_mutating_wire(monkeypatch):
    from simstack.runner_api.datasets import DatasetRunnerClient
    from simstack.core.runner_execution import hydration
    monkeypatch.setattr(hydration,"_model_class",lambda mapping:IntData)
    entry = {"variable_mapping":f"{IntData.__module__}.{IntData.__qualname__}",
        "value":{"_id":"1"*24,"value":3},"dependencies":[]}
    restored = DatasetRunnerClient(None).hydrate_documents([entry])
    assert restored["1"*24].value == 3
    assert "reference" not in entry and "id" not in entry["value"]


@pytest.mark.parametrize("value,reference", [({},None),({"id":None},None),({"id":1},None),
    ({"id":"A"*24},None),({"id":"invalid"},None),({"id":"1"*24,"_id":"1"*24},None),
    ({"id":"1"*24},"2"*24)])
def test_dataset_row_document_converter_rejects_noncanonical_or_conflicting_identity(value,reference):
    from simstack.runner_api.datasets import DatasetRunnerClient
    from simstack.core.runner_execution.errors import RunnerTaskPayloadError
    entry = {"variable_mapping":"simstack.models.base_types.IntData","value":value,"dependencies":[]}
    if reference is not None:
        entry["reference"] = reference
    with pytest.raises(RunnerTaskPayloadError,match="identity|canonical"):
        DatasetRunnerClient(None).hydrate_documents([entry])
