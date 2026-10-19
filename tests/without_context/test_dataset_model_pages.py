"""Typed snapshot iteration stays bounded and never attaches a section cache."""
from __future__ import annotations

import copy
import json

import pytest

from simstack.core.runner_execution.serialization import _model_document
from simstack.models import (
    DataSet, DataSetMetadata, DataSetSection, DataSetTupleSection, DatasetModelPage, DatasetModelRow,
    FileStack, IntData, StringData,
)
from simstack.models.dataset_storage import dataset_io_scope, dataset_source_graph_hash
from simstack.models.document import Model, Reference
from simstack.runner_api.datasets import DatasetRunnerClient


class LinkedValue(Model):
    value: IntData = Reference()


class ArtifactCell(Model):
    value: IntData = Reference()
    file: FileStack = Reference()
    nested: DataSet = Reference()


def mapping(model):
    cls = model if isinstance(model, type) else type(model)
    return f"{cls.__module__}.{cls.__qualname__}"


def document(model):
    dependencies = []
    if isinstance(model, LinkedValue):
        dependencies.append({"variable_mapping": mapping(model.value),
                             "reference": str(model.value.id), "value": _model_document(model.value)})
    return {"variable_mapping": mapping(model), "value": _model_document(model),
            "dependencies": dependencies}


def row(index, *, kind="dict", sparse=False, model=None):
    models = [model or IntData(id=f"{index + 1:024x}", value=index)]
    if not sparse:
        models.append(StringData(id=f"{index + 10000:024x}", value=f"label-{index}"))
    slots = ["value", "label"] if kind == "dict" else ["0", "1"]
    documents = [document(value) for value in models]
    refs = [{"slot": slot, "reference": str(value.id), "variable_mapping": mapping(value),
             "graph_hash": dataset_source_graph_hash(entry)}
            for slot, value, entry in zip(slots, models, documents)]
    return {"row_id": f"{index + 100000:024x}", "position": index,
            "row_name": f"row-{index}" if kind == "dict" else None,
            "refs": list(reversed(refs)), "documents": documents}


class ServerPages:
    def __init__(self, rows):
        self.rows = rows
        self.requests = []
        self.hydrations = []
        self.client = DatasetRunnerClient(None)

    def page(self, storage, *, kind, section_key, offset, limit):
        self.requests.append((offset, limit))
        rows = copy.deepcopy(self.rows[offset:offset + limit])
        next_offset = offset + len(rows)
        return {"version": 1, "revision": storage.revision, "row_count": len(self.rows),
                "offset": offset, "rows": rows,
                "next_offset": next_offset if next_offset < len(self.rows) else None,
                "truncated": next_offset < len(self.rows)}

    def hydrate_documents(self, entries):
        self.hydrations.append(len(entries))
        return self.client.hydrate_documents(entries)


@pytest.fixture(autouse=True)
def installed_catalogue(monkeypatch):
    from simstack.core.runner_execution import hydration
    catalogue = {mapping(cls): cls for cls in (IntData, StringData, LinkedValue)}
    monkeypatch.setattr(hydration, "_model_class", catalogue.__getitem__)


def section(rows, kind="dict"):
    cls = DataSetSection if kind == "dict" else DataSetTupleSection
    types = {"value": "IntData", "label": "StringData"} if kind == "dict" else ["IntData", "StringData"]
    return cls(model_types=types, storage={"dataset_id": "1" * 24, "section_id": "2" * 24,
               "revision": "3" * 64, "row_count": len(rows)}), ServerPages(rows)


@pytest.mark.parametrize("kind", ["dict", "tuple"])
def test_typed_pages_use_actual_server_documents_and_hydrate_only_requested_rows(kind):
    rows = [row(index, kind=kind) for index in range(205)]
    target, provider = section(rows, kind)
    assert all("reference" not in entry for raw in rows for entry in raw["documents"])
    with dataset_io_scope(provider):
        pages = target.iter_model_pages(limit=100)
        assert provider.requests == []
        first = next(pages)
        assert isinstance(first, DatasetModelPage) and len(first) == 100
        assert first.revision == target.storage.revision
        assert first.offset == 0 and first.row_count == 205 and first.next_offset == 100
        assert provider.requests == [(0, 100)] and provider.hydrations == [2] * 100
        assert "_cache" not in target.__dict__
        assert isinstance(first.rows[0], DatasetModelRow)
        for index, item in enumerate(first):
            assert item.row_id == rows[index]["row_id"] and item.position == index
            if kind == "dict":
                assert item.name == f"row-{index}"
                assert item.models["value"].value == index
                assert item.models["label"].value == f"label-{index}"
            else:
                assert item.name is None
                number, label = item.models
                assert isinstance(item.models, tuple)
                assert number.value == index and label.value == f"label-{index}"
        second, last = list(pages)
        assert second.offset == 100 and second.next_offset == 200
        assert last.offset == 200 and len(last) == 5 and last.next_offset is None
        assert provider.requests == [(0, 100), (100, 100), (200, 100)]
    assert "_cache" not in target.__dict__ and "_dataset_row_ids" not in target.__dict__
    assert target.model_dump()["data"] == ({} if kind == "dict" else [])
    assert target.table_entries == []


def test_iter_models_flattens_pages_preserves_sparse_named_rows_and_detaches_edits():
    target, provider = section([row(index, sparse=True) for index in range(5)])
    before = target.model_dump()
    with dataset_io_scope(provider):
        iterator = target.iter_models(limit=2)
        first = next(iterator)
        assert list(first.models) == ["value"] and first.name == "row-0"
        first.models["value"].value = 999
        assert provider.requests == [(0, 2)]
        assert [item.position for item in iterator] == [1, 2, 3, 4]
        assert [item.models["value"].value for item in target.iter_models(limit=3)] == list(range(5))
    assert target.model_dump() == before and "_cache" not in target.__dict__


def test_one_page_keeps_historical_versions_of_same_root_and_dependency_ids_separate():
    versions = [LinkedValue(id="a" * 24, value=IntData(id="b" * 24, value=value)) for value in (1, 99)]
    target, provider = section([row(index, sparse=True, model=model) for index, model in enumerate(versions)])
    target.model_types = {"value": "LinkedValue"}
    with dataset_io_scope(provider):
        items = list(next(target.iter_model_pages()))
    first, second = (item.models["value"] for item in items)
    assert first.id == second.id and first is not second
    assert first.value.id == second.value.id and first.value is not second.value
    assert [first.value.value, second.value.value] == [1, 99]
    assert provider.requests == [(0, 100)] and "_cache" not in target.__dict__


@pytest.mark.parametrize("mutation,match", [
    (lambda raw: raw.update(row_id="invalid"), "row ID"),
    (lambda raw: raw["refs"][0].update(reference="invalid"), "model reference"),
    (lambda raw: raw["documents"].clear(), "missing or extra"),
    (lambda raw: raw["documents"].append(document(IntData(id="f" * 24, value=1))), "missing or extra"),
    (lambda raw: raw["documents"][0]["value"].update(id="f" * 24), "missing or extra"),
    (lambda raw: raw["refs"][0].update(variable_mapping=mapping(StringData)), "reference type"),
    (lambda raw: raw["refs"].append(copy.deepcopy(raw["refs"][0])), "duplicate slot"),
    (lambda raw: raw["refs"][0].update(slot="unknown"), "section types"),
    (lambda raw: raw["documents"][0]["value"].update(value=999), "graph disagrees"),
])
def test_typed_pages_reject_invalid_identity_type_missing_extra_and_graph(mutation, match):
    raw = row(0, sparse=True)
    mutation(raw)
    target, provider = section([raw])
    with dataset_io_scope(provider), pytest.raises(ValueError, match=match):
        next(target.iter_model_pages())
    assert "_cache" not in target.__dict__


@pytest.mark.parametrize("kind,mutation,match", [
    ("dict", lambda rows: rows[1].update(row_id=rows[0]["row_id"]), "duplicate row IDs"),
    ("dict", lambda rows: rows[1].update(row_name=rows[0]["row_name"]), "row names"),
    ("dict", lambda rows: rows[0].update(row_name=None), "row names"),
    ("tuple", lambda rows: rows[0]["refs"].pop(), "section types"),
    ("tuple", lambda rows: rows[0].update(row_name="named"), "row name"),
])
def test_typed_page_membership_validation(kind, mutation, match):
    rows = [row(index, kind=kind) for index in range(2)]
    mutation(rows)
    target, provider = section(rows, kind)
    with dataset_io_scope(provider), pytest.raises(ValueError, match=match):
        next(target.iter_model_pages())


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_typed_pages_keep_existing_page_limit_contract(limit):
    target, provider = section([row(0)])
    with dataset_io_scope(provider), pytest.raises(ValueError, match="between 1 and 100"):
        next(target.iter_model_pages(limit))


def test_remote_pages_require_snapshot_and_scope_and_empty_section_has_no_reads():
    with pytest.raises(RuntimeError, match="normalized dataset snapshots"):
        next(DataSetSection().iter_row_pages())
    assert list(DataSetSection().iter_model_pages()) == []
    target, provider = section([row(0)])
    with pytest.raises(RuntimeError, match="scoped dataset provider"):
        next(target.iter_models())
    empty, empty_provider = section([])
    with dataset_io_scope(empty_provider):
        assert list(empty.iter_models()) == []
    assert empty_provider.requests == [] and empty_provider.hydrations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dict", "tuple"])
async def test_new_sections_use_bounded_detached_typed_pages_without_provider(kind, monkeypatch):
    target = DataSetSection() if kind == "dict" else DataSetTupleSection()
    for index in range(205):
        model = LinkedValue(value=IntData(value=index))
        if kind == "dict":
            target.add_row({"value": model}, name=f"row-{index}")
        else:
            await target.append((model,))
    cache = target._get_cache()
    identities = copy.deepcopy(target.__dict__["_dataset_row_ids"])

    def no_full_load(*args):
        raise AssertionError("typed buffer iteration must not call the full-section accessor")

    copied_values = []
    original_copy = LinkedValue.__deepcopy__

    def track_copy(model, memo=None):
        copied_values.append(model.value.value)
        return original_copy(model, memo)

    monkeypatch.setattr(type(target), "_get_cache", no_full_load)
    monkeypatch.setattr(LinkedValue, "__deepcopy__", track_copy)
    pages = target.iter_model_pages(limit=100)
    first = next(pages)
    assert copied_values == list(range(100))
    assert first.revision is None and first.row_count == 205 and first.next_offset == 100
    assert len(first) == 100 and first.offset == 0
    for index, item in enumerate(first):
        assert item.position == index
        assert item.name == (f"row-{index}" if kind == "dict" else None)
        assert item.row_id == (identities[f"row-{index}"] if kind == "dict" else identities[index])
        model = item.models["value"] if kind == "dict" else item.models[0]
        model.value.value = -1
        original = cache[f"row-{index}"]["value"] if kind == "dict" else cache[index][0]
        assert original.value.value == index
    assert [(page.offset, len(page), page.next_offset) for page in pages] == [(100, 100, 200), (200, 5, None)]
    assert copied_values == list(range(205))
    assert target.__dict__["_cache"] is cache and target.storage is None
    assert [item.row_id for item in target.iter_models(limit=37)] == (
        list(identities.values()) if kind == "dict" else identities
    )


@pytest.mark.parametrize("kind", ["dict", "tuple"])
@pytest.mark.asyncio
async def test_typed_pages_preserve_working_buffer_edits_instead_of_rereading_snapshot(kind):
    target, provider = section([row(index, kind=kind) for index in range(3)], kind)
    with dataset_io_scope(provider):
        cache = target._get_cache()
    first = cache["row-0"]["value"] if kind == "dict" else cache[0][0]
    first.value = 999
    if kind == "dict":
        target.pop("row-2")
    else:
        await target.pop()
    requests = provider.requests.copy()
    with dataset_io_scope(provider):
        page = next(target.iter_model_pages(limit=100))
    assert page.revision is None and page.row_count == 2 and len(page) == 2
    values = [item.models["value"].value if kind == "dict" else item.models[0].value for item in page]
    assert values == [999, 1] and provider.requests == requests
    copied = page.rows[0].models["value"] if kind == "dict" else page.rows[0].models[0]
    copied.value = -1
    assert first.value == 999


@pytest.mark.parametrize("kind", ["dict", "tuple"])
def test_typed_buffer_pages_reject_unhydrated_legacy_references(kind):
    target = (DataSetSection(model_types={"value": "IntData"}, data={"row": {"value": "a" * 24}})
              if kind == "dict" else DataSetTupleSection(model_types=["IntData"], data=[["a" * 24]]))
    with pytest.raises(RuntimeError, match="references must be hydrated"):
        next(target.iter_models())
    assert "_cache" not in target.__dict__


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_typed_inline_pages_validate_limits(limit):
    with pytest.raises(ValueError, match="between 1 and 100"):
        next(DataSetSection().iter_model_pages(limit))


@pytest.mark.parametrize("mode", ["identity", "missing", "extra"])
def test_provider_hydration_cannot_substitute_or_leak_models(mode):
    target, provider = section([row(0, sparse=True)])
    restored = IntData(id="f" * 24, value=1)
    provider.hydrate_documents = lambda entries: (
        {} if mode == "missing" else {"1".zfill(24): restored} if mode == "identity"
        else {"1".zfill(24): IntData(id="1".zfill(24), value=0), str(restored.id): restored}
    )
    with dataset_io_scope(provider), pytest.raises(ValueError, match="identity|missing or extra"):
        next(target.iter_model_pages())


@pytest.mark.parametrize("kind", ["dict", "tuple"])
def test_prepared_artifact_typed_pages_expose_roots_only_and_keep_row_graph_versions(tmp_path, monkeypatch, kind):
    from simstack.workflows import runtime
    from simstack.workflows.dataset_io import PreparedDatasetIO

    classes = {mapping(cls): cls for cls in (ArtifactCell, IntData, FileStack, DataSet)}
    monkeypatch.setattr(runtime, "_runtime_classes", lambda request: (classes, [], None))
    rows = []
    for index, value in enumerate((1, 99)):
        nested = DataSet(id="d" * 24, metadata=DataSetMetadata(field_name="nested"),
            sections={"inner": DataSetSection(model_types={"value": "IntData"}, storage={
                "dataset_id": "d" * 24, "section_id": "e" * 24, "revision": "f" * 64,
                "row_count": 1})})
        file = FileStack.from_bytes(f"version-{value}".encode(), "payload.txt")
        file.id = "c" * 24
        cell = ArtifactCell(id="a" * 24, value=IntData(id="b" * 24, value=value), file=file, nested=nested)
        entry = {"variable_mapping": mapping(cell), "value": _model_document(cell), "dependencies": [
            {"variable_mapping": mapping(child), "reference": str(child.id), "value": _model_document(child)}
            for child in (cell.value, cell.file, cell.nested)]}
        rows.append({"row_id": f"{index + 1:024x}", "position": index,
            "row_name": f"cell-{index}" if kind == "dict" else None,
            "refs": [{"slot": "value" if kind == "dict" else "0", "reference": str(cell.id),
                      "variable_mapping": mapping(cell), "graph_hash": dataset_source_graph_hash(entry)}],
            "documents": [entry]})
    target, _ = section(rows, kind)
    target.model_types = {"value": "ArtifactCell"} if kind == "dict" else ["ArtifactCell"]
    source = tmp_path / "page.json"
    source.write_text(json.dumps({"version": 1, "revision": target.storage.revision, "row_count": 2,
        "offset": 0, "rows": rows, "next_offset": None}))
    provider = PreparedDatasetIO({"files_root": str(tmp_path), "dataset_io": {"sections": [{
        "dataset_id": str(target.storage.dataset_id), "section_id": target.storage.section_id,
        "revision": target.storage.revision, "kind": kind,
        "pages": [{"offset": 0, "row_count": 2, "path": "page.json"}]}]}})
    roots = provider.hydrate_documents(rows[0]["documents"])
    assert set(roots) == {"a" * 24}
    assert roots["a" * 24].nested.sections["inner"].__dict__["_dataset_section_key"] == "inner"
    with dataset_io_scope(provider):
        items = list(next(target.iter_model_pages(limit=2)))
        repeated = list(target.iter_models(limit=1))
    models = [item.models["value"] if kind == "dict" else item.models[0] for item in items]
    assert [model.value.value for model in models] == [1, 99]
    assert [model.file.read_bytes() for model in models] == [b"version-1", b"version-99"]
    assert models[0].id == models[1].id and models[0] is not models[1]
    assert models[0].value.id == models[1].value.id and models[0].value is not models[1].value
    assert models[0].file.id == models[1].file.id and models[0].file is not models[1].file
    assert [item.row_id for item in items] == [raw["row_id"] for raw in rows]
    assert [item.position for item in repeated] == [0, 1]
    assert "_cache" not in target.__dict__ and target.table_entries == []
