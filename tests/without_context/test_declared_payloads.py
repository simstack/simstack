"""Payload contracts, lossless row storage and runtime preparation boundaries."""
from __future__ import annotations

import io
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import ClassVar

import numpy as np
import pyarrow.parquet as pq
import pytest

from simstack.models.array_storage import ArrayStorage
from simstack.models.document import Model, PAYLOAD_SCHEMA_KEY, Reference
from simstack.models.files import FileStack
from simstack.models.images2d import Image2DArtifactModel
from simstack.models.payloads import (
    MAX_TABLE_ROW_BYTES, TABLE_PREVIEW_BYTES, TABLE_PREVIEW_ROWS, encode_table_rows,
    prepare_payload_document_fields, prepare_payloads, read_table_rows, table_metadata,
)
from simstack.models.simple_table import SimpleTable
from simstack.models.table_artifact import AGGridColumnDef, TableArtifactModel


@pytest.mark.parametrize("model,field", [
    (SimpleTable(name="table", row=[{"a": 1}, {"a": 2}]), "row"),
    (TableArtifactModel(row_data=[{"a": 1}, {"a": 2}]), "row_data"),
])
def test_table_preparation_keeps_full_python_rows_and_only_bounded_preview_in_document(model, field):
    original = getattr(model, field)
    model.prepare_blob_payload()
    assert getattr(model, field) == original
    assert model.payload.row_count == 2
    assert model.payload.columns == ["a"]
    assert model.model_dump_doc()[field] == []
    assert model.model_dump_doc()["payload"]["file"] == str(model.payload.file.id)
    assert model.preview_rows == original and not model.preview_truncated
    file_id = model.payload.file.id
    model.prepare_blob_payload()
    assert model.payload.file.id == file_id
    getattr(model, field).append({"a": 3})
    model.prepare_blob_payload()
    assert model.payload.row_count == 3 and model.payload.file.id != file_id
    assert model.get_rows(1, 1) == [{"a": 2}]
    assert list(model.iter_rows())[-1] == {"a": 3}
    setattr(model, field, [{"new": "value"}])
    assert model.payload is None and getattr(model, field) == [{"new": "value"}]


def test_table_roundtrip_loads_full_rows_not_preview_and_keeps_legacy_read():
    rows = [{"n": index, "label": f"row-{index}"} for index in range(100)]
    model = SimpleTable(row=rows)
    model.prepare_blob_payload()
    assert len(model.preview_rows) == TABLE_PREVIEW_ROWS and model.preview_truncated
    wire = model.model_dump_doc()
    wire["payload"]["file"] = model.payload.file.model_dump_doc()
    restored = SimpleTable.model_validate_doc(wire)
    assert restored.row == rows
    assert restored.row is restored.row
    assert SimpleTable.model_validate_doc({"row": rows}).row == rows


def test_table_preview_byte_budget_and_large_rows_have_explicit_limit():
    model = SimpleTable(row=[{"text": "x" * TABLE_PREVIEW_BYTES}])
    model.prepare_blob_payload()
    assert model.preview_rows == [] and model.preview_truncated
    assert len(model.row[0]["text"]) == TABLE_PREVIEW_BYTES
    with pytest.raises(ValueError, match="row exceeds"):
        encode_table_rows([{"text": "x" * MAX_TABLE_ROW_BYTES}])


def test_empty_table_preserves_declared_columns_and_metadata():
    for model in (SimpleTable(heading=["a", "b"]), TableArtifactModel(columns_defs=[AGGridColumnDef(field="a", headerName="A")])):
        model.prepare_blob_payload()
        assert model.payload.row_count == 0
        assert model.payload.columns
        assert table_metadata(model.payload.file.read_bytes())["columns"] == model.payload.columns
        assert model.get_rows() == []


def test_parquet_typed_rows_are_lossless_and_footer_contract_is_explicit():
    rows = [{"int": 7, "float": float("nan"), "bool": False, "null": None,
             "text": "юникод", "binary": b"\0\xff", "date": date(2026, 1, 2),
             "time": datetime(2026, 1, 2, tzinfo=timezone.utc), "decimal": Decimal("12.50"),
             "nested": {"values": [1, 2]}, "array": ["a", "b"]}]
    raw = encode_table_rows(rows)
    restored = read_table_rows(io.BytesIO(raw))[0]
    assert math.isnan(restored.pop("float"))
    assert restored == {key: value for key, value in rows[0].items() if key != "float"}
    metadata = pq.ParquetFile(io.BytesIO(raw)).schema_arrow.metadata
    assert metadata[b"simstack.payload.codec"] == b"parquet-rows"
    assert metadata[b"simstack.payload.version"] == b"1"
    assert raw == encode_table_rows(rows)


@pytest.mark.parametrize("rows", [
    [{"a": 1}, {"a": 1.5}], [{"a": 1}, {"b": 2}],
    [{"a": (1, 2)}], [{"a": object()}], [{"a": {1: "x"}}],
    [{"a": {"x": 1}}, {"a": {"y": 1}}], [{"a": 2**80}],
    [{"a": datetime(2026, 1, 2, fold=1)}],
    [{"a": Decimal("1.0")}, {"a": Decimal("1.00")}],
])
def test_table_rejects_coercion_missing_keys_and_unsupported_values(rows):
    with pytest.raises(ValueError):
        encode_table_rows(rows)


def test_reader_reads_only_intersecting_row_groups_and_column_projection():
    raw = encode_table_rows([{"i": i, "value": "x"} for i in range(2500)])
    parquet = pq.ParquetFile(io.BytesIO(raw))
    assert parquet.num_row_groups == 3
    assert max(parquet.metadata.row_group(i).num_rows for i in range(3)) <= 1024
    assert read_table_rows(raw, 1020, 10, ["i"]) == [{"i": i} for i in range(1020, 1030)]
    assert read_table_rows(raw, 5000, 10) == []
    with pytest.raises(ValueError, match="columns"):
        read_table_rows(raw, columns=["missing"])


class DerivedArray(ArrayStorage):
    extra_label: str = "scientific"


class DerivedTable(SimpleTable):
    extra_label: str = "scientific"


class DeclaredBinary(Model):
    __payload_fields__: ClassVar[tuple[dict, ...]] = ({
        "source": "data", "target": "file", "codec": "bytes", "codec_version": 1,
    },)
    data: bytes = b""
    file: FileStack | None = Reference(None)
    metadata: dict = {}


class NestedPayloads(Model):
    array: DerivedArray
    tables: list[DerivedTable]
    binary: DeclaredBinary
    image: Image2DArtifactModel
    arbitrary: dict


def make_nested():
    array = DerivedArray(name="array")
    array.array = np.array([1, 2], dtype=np.int16)
    return NestedPayloads(array=array, tables=[DerivedTable(row=[{"x": 1}])],
                          binary=DeclaredBinary(data=b"explicit", metadata={"data": b"untouched"}),
                          image=Image2DArtifactModel(name="image", format="png", data=b"pixels"),
                          arbitrary={"data": b"unmarked"})


def test_recursive_declarations_include_subclasses_and_leave_unmarked_values_unchanged():
    model = make_nested()
    prepare_payloads([model])
    assert model.array.data_json is None and model.array.payload_file is not None
    assert model.tables[0].inline_rows == [] and model.tables[0].row == [{"x": 1}]
    assert model.binary.data == b"" and model.binary.file.read_bytes() == b"explicit"
    assert model.binary.metadata["data"] == b"untouched"
    assert model.arbitrary["data"] == b"unmarked"
    assert model.image.data == b"pixels" and model.image.inline_data == b""
    schema = NestedPayloads.model_json_schema()
    assert schema["$defs"]["DerivedArray"][PAYLOAD_SCHEMA_KEY]["fields"][0]["codec"] == "npy"
    assert schema["$defs"]["DerivedTable"][PAYLOAD_SCHEMA_KEY]["fields"][0]["codec"] == "parquet-rows"
    assert schema["$defs"]["TablePayload"]["properties"]["file"]["x-simstack-reference"]


def test_retry_namespace_produces_identical_payload_ids_and_reuses_prepared_payloads():
    models = [make_nested(), make_nested()]
    for model in models:
        prepare_payloads([model], publication_namespace="a" * 64)
    for selector in (lambda m: m.array.payload_file, lambda m: m.tables[0].payload.file,
                     lambda m: m.binary.file, lambda m: m.image.payload_file):
        assert selector(models[0]).id == selector(models[1]).id
        previous = selector(models[0]).id
        prepare_payloads([models[0]], publication_namespace="a" * 64)
        assert selector(models[0]).id == previous


def test_server_codec_prepares_raw_documents_without_workflow_class_imports():
    declaration = DerivedTable.model_json_schema()[PAYLOAD_SCHEMA_KEY]
    raw = {"row": [{"a": 1}], "extra_label": "preserved"}
    value = prepare_payload_document_fields(raw, declaration)
    assert value["row"] == [] and value["extra_label"] == "preserved"
    assert value["payload"].file.read_bytes()
    assert raw["row"] == [{"a": 1}]
    with pytest.raises(ValueError, match="version"):
        prepare_payload_document_fields(raw, {"version": 2, "fields": []})


def test_logical_hash_ignores_preparation_reload_locations_and_preview_but_detects_mutation():
    from simstack.core.node import compute_arg_hash
    from simstack.core.hash import complex_hash_function
    from simstack.models.document import ObjectId
    from simstack.models.files import BlobReference
    from simstack.models.payloads import table_logical_hash
    rows = [{"a": i, "b": str(i)} for i in range(30)]
    model = SimpleTable(heading=["a", "b"], row=rows)
    original = compute_arg_hash([model])
    original_complex = complex_hash_function(model)
    model.prepare_blob_payload()
    assert model.payload.logical_hash == table_logical_hash(rows, ["a", "b"])
    assert compute_arg_hash([model]) == original
    assert complex_hash_function(model) == original_complex
    wire = model.model_dump_doc()
    wire["payload"]["file"] = model.payload.file.model_dump_doc()
    restored = SimpleTable.model_validate_doc(wire)
    assert compute_arg_hash([restored]) == original
    restored.payload.file.id = ObjectId()
    restored.payload.file.blob = BlobReference(id=str(restored.payload.file.id), size_bytes=10, checksum_sha256="a" * 64)
    restored.preview_rows = []
    assert compute_arg_hash([restored]) == original
    # Local model keeps its original inline file for this read/mutation exercise.
    model.row[0]["a"] = 1000
    assert compute_arg_hash([model]) != original
    model.prepare_blob_payload()
    assert model.payload.logical_hash != restored.payload.logical_hash


def test_logical_hash_is_typed_and_descriptor_digest_is_verified_on_full_read():
    from simstack.models.payloads import table_logical_hash
    assert table_logical_hash([{"x": True}]) != table_logical_hash([{"x": 1}])
    assert table_logical_hash([{"x": 1}]) != table_logical_hash([{"x": 1.0}])
    assert table_logical_hash([{"x": -0.0}]) != table_logical_hash([{"x": 0.0}])
    assert table_logical_hash([], ["x"]) != table_logical_hash([], ["y"])
    model = SimpleTable(row=[{"x": 1}])
    model.prepare_blob_payload()
    wire = model.model_dump_doc()
    wire["payload"]["file"] = model.payload.file.model_dump_doc()
    wire["payload"]["logical_hash"] = "a" * 64
    restored = SimpleTable.model_validate_doc(wire)
    with pytest.raises(ValueError, match="logical hash"):
        _ = restored.row


def test_existing_payload_rejects_conflicting_inline_content_and_unknown_footer():
    declaration = SimpleTable.model_json_schema()[PAYLOAD_SCHEMA_KEY]
    with pytest.raises(ValueError, match="both inline"):
        prepare_payload_document_fields({"row": [{"a": 1}], "payload": {"file": "id"}}, declaration)
    import pyarrow as pa
    raw = io.BytesIO()
    pq.write_table(pa.table({"x": [1]}), raw)
    with pytest.raises(ValueError, match="codec or version"):
        table_metadata(raw.getvalue())
    assert "row" in SimpleTable(row=[]).model_dump()
    assert "inline_rows" not in SimpleTable(row=[]).model_dump()


def test_reading_a_prepared_table_does_not_reencode_or_restage_unchanged_payload(monkeypatch):
    model = SimpleTable(row=[{"x": 1}])
    prepare_payloads([model])
    identifier = model.payload.file.id
    assert model.row == [{"x": 1}]
    def unexpected_encode(*args, **kwargs):
        raise AssertionError("Unchanged payload was encoded twice")
    monkeypatch.setattr("simstack.models.payloads.encode_table_rows", unexpected_encode)
    prepare_payloads([model])
    model.prepare_blob_payload()
    assert model.payload.file.id == identifier


def test_serialized_dependency_closure_keeps_embedded_models_inline_and_emits_only_reference_edges(monkeypatch):
    from types import SimpleNamespace
    from simstack.core.runner_execution import serialization
    class Output(Model):
        table: SimpleTable = Reference()
        nested: dict[str, DerivedTable]
        binary: DeclaredBinary
    value = Output(table=SimpleTable(row=[{"x": 1}]),
                   nested={"small": DerivedTable(row=[{"x": 2}])},
                   binary=DeclaredBinary(data=b"explicit bytes"))
    prepare_payloads([value])
    monkeypatch.setattr(serialization, "_mapping_for_model", lambda model: SimpleNamespace(
        mapping=f"{type(model).__module__}.{type(model).__name__}"))
    entry = serialization._result_entry(value, variable_name="output")
    assert entry["value"]["table"] == str(value.table.id)
    assert isinstance(entry["value"]["nested"]["small"], dict)
    assert isinstance(entry["value"]["binary"], dict)
    assert {item["reference"] for item in entry["dependencies"]} == {
        str(value.table.id), str(value.table.payload.file.id),
        str(value.nested["small"].payload.file.id), str(value.binary.file.id),
    }
    assert str(value.nested["small"].id) not in {item["reference"] for item in entry["dependencies"]}
    assert str(value.binary.id) not in {item["reference"] for item in entry["dependencies"]}


def test_reference_containers_and_inline_object_lists_keep_transitive_dependencies():
    from simstack.core.runner_execution.serialization import _dependent_models
    from simstack.models.file_list import FileList
    class Output(Model):
        files: FileList
        refs: dict[str, list[FileStack | None]] = Reference(default_factory=dict)
        inline: dict
    file = FileStack.from_bytes(b"first", "first.bin")
    other = FileStack.from_bytes(b"other", "other.bin")
    unreferenced = FileStack.from_bytes(b"inline", "inline.bin")
    value = Output(files=FileList(elements=[file]), refs={"a": [other, None]}, inline={"file": unreferenced})
    assert {str(item.id) for item in _dependent_models(value)} == {str(file.id), str(other.id)}
