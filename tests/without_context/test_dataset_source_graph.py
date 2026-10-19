"""Reference closure identity changes even when the source root stays unchanged."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from simstack.core.hash import complex_hash_function
from simstack.core.runner_execution.serialization import _model_document
from simstack.models import DataSetSection, IntData
from simstack.models.dataset_storage import (
    dataset_io_scope, dataset_section_revision, dataset_source_graph_hash,
    dataset_source_hash, section_rows,
)
from simstack.models.document import Model, Reference
from simstack.runner_api.datasets import DatasetRunnerClient


class SourceRoot(Model):
    child: IntData = Reference()


def mapping(model):
    cls = model if isinstance(model, type) else type(model)
    return f"{cls.__module__}.{cls.__qualname__}"


def test_graph_hash_normalizes_storage_precision_aliases_order_and_metadata():
    document = {"variable_mapping": "model.Root", "value": {"id": "1" * 24,
        "date": datetime(2026, 10, 19, 12, 0, 0, 123999, tzinfo=timezone.utc)},
        "dependencies": [
            {"variable_mapping": "model.Value", "value": {"id": "2" * 24, "value": 2 ** 60}},
            {"variable_mapping": "model.Value", "value": {"id": "3" * 24, "value": b"bytes"}},
        ]}
    equivalent = copy.deepcopy(document)
    equivalent["value"]["_id"] = equivalent["value"].pop("id")
    equivalent["value"]["date"] = {"$date": "2026-10-19T12:00:00.123000+00:00"}
    equivalent["variable_name"] = "ignored"
    equivalent["dependencies"][0]["value"]["value"] = {"$numberLong": str(2 ** 60)}
    equivalent["dependencies"][1]["reference"] = "3" * 24
    equivalent["dependencies"].reverse()
    equivalent["dependencies"].append(copy.deepcopy(equivalent["dependencies"][0]))
    assert dataset_source_graph_hash(document) == dataset_source_graph_hash(equivalent)
    equivalent["dependencies"][1]["value"]["value"] = 10
    assert dataset_source_graph_hash(document) != dataset_source_graph_hash(equivalent)


def test_dependency_only_mutation_changes_section_hash_and_revision_without_root_change():
    root = SourceRoot(child=IntData(value=7))
    section = DataSetSection()
    section.add_row({"value": root}, "one")
    root_hash = dataset_source_hash(_model_document(root))
    previous_rows = list(section_rows(section))
    previous_hash = complex_hash_function(section)
    previous_revision = dataset_section_revision(section.model_types, previous_rows)
    root.child.value = 99
    changed_rows = list(section_rows(section))
    assert dataset_source_hash(_model_document(root)) == root_hash
    assert previous_rows[0]["refs"][0]["source_hash"] == changed_rows[0]["refs"][0]["source_hash"]
    assert previous_rows[0]["refs"][0]["graph_hash"] != changed_rows[0]["refs"][0]["graph_hash"]
    assert dataset_section_revision(section.model_types, changed_rows) != previous_revision
    assert complex_hash_function(section) != previous_hash


@pytest.mark.parametrize("legacy", [False, True])
def test_loading_snapshot_preserves_hash_but_dependency_edit_changes_it(monkeypatch, legacy):
    from simstack.core.runner_execution import hydration
    catalogue = {mapping(cls): cls for cls in (IntData, SourceRoot)}
    monkeypatch.setattr(hydration, "_model_class", catalogue.__getitem__)
    original = DataSetSection()
    root = SourceRoot(child=IntData(value=7))
    original.add_row({"value": root}, "one")
    rows = list(section_rows(original))
    if legacy:
        rows[0]["refs"][0].pop("graph_hash")
    revision = dataset_section_revision(original.model_types, rows)
    target = DataSetSection(model_types=original.model_types, storage={
        "dataset_id": "1" * 24, "section_id": "2" * 24, "revision": revision, "row_count": 1})
    rows[0]["documents"] = [{"variable_mapping": mapping(root), "value": _model_document(root),
        "dependencies": [{"variable_mapping": mapping(root.child), "reference": str(root.child.id),
                          "value": _model_document(root.child)}]}]

    class Provider:
        hydrate_documents = DatasetRunnerClient(None).hydrate_documents

        def page(self, storage, **kwargs):
            return {"version": 1, "revision": revision, "row_count": 1, "offset": 0,
                    "rows": copy.deepcopy(rows), "next_offset": None}

    before = complex_hash_function(target)
    with dataset_io_scope(Provider()):
        loaded = target["one"]["value"]
    assert complex_hash_function(target) == before
    assert dataset_section_revision(target.model_types, section_rows(target)) == revision
    loaded.child.value = 99
    assert complex_hash_function(target) != before
    assert dataset_section_revision(target.model_types, section_rows(target)) != revision
    assert "graph_hash" in list(section_rows(target))[0]["refs"][0]


@pytest.mark.parametrize("mutation", [
    lambda entry: entry.update(variable_mapping=""),
    lambda entry: entry["value"].update(id="invalid"),
    lambda entry: entry["value"].update(_id="1" * 24),
    lambda entry: entry.update(reference="2" * 24),
    lambda entry: entry.update(dependencies={}),
    lambda entry: entry["dependencies"].append({"variable_mapping": "model.Root",
                                             "value": {"id": "1" * 24, "value": 99}}),
])
def test_graph_hash_rejects_malformed_or_conflicting_identity(mutation):
    entry = {"variable_mapping": "model.Root", "value": {"id": "1" * 24, "value": 1},
             "dependencies": []}
    mutation(entry)
    with pytest.raises(ValueError):
        dataset_source_graph_hash(entry)
