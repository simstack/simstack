from datetime import datetime, timezone
from decimal import Decimal
import json
import types
from uuid import UUID

import pytest
from pydantic import computed_field, field_serializer, model_serializer

from simstack.models import FloatData, StringData
from simstack.models.document import EmbeddedModel, Field, Model, Reference
from simstack.models.document.models import _MODEL_TYPES
from simstack.models.dataset import DataSet, DataSetSection, DataSetSelection, DataSetSelectionField
from simstack.models.datasettuple import DataSetTuple, DataSetTupleSection
from simstack.models.dataset_metadata import DataSetMetadata, validate_metadata_template
from simstack.models.file_list import FileListModel
from simstack.models.files import FileStack
from simstack.models.array_list import ArrayList
from simstack.models.document import ObjectId
from simstack.server_api.scientific_values import ScientificValuesAPI
from simstack.server_api.scientific_value_graph import decode_graph, encode_graph


class ScientificBase(Model):
    value: float


class ScientificSubtype(ScientificBase):
    unit: str


class ScientificEnvelope(Model):
    first: ScientificBase = Reference()
    second: ScientificBase = Reference()
    optional: ScientificBase | None = Reference(default=None)
    pair: tuple[ScientificBase, str]
    metadata: dict = Field(default_factory=dict)


def decode(graph):
    return decode_graph(graph, lambda name: _MODEL_TYPES[name])


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["builder", "prepared", "decoded"])
async def test_dataset_scientific_rows_invalidate_node_cache_before_publication(state):
    from simstack.core.node import compute_arg_hash

    row = FloatData(value=1)
    section = DataSetSection()
    section.add_row({"signal": row}, name="first")
    value = DataSet(metadata=DataSetMetadata(field_name="experiment"), sections={"measurements": section})
    if state != "builder":
        await section.prepare_publication()
    if state == "decoded":
        value = decode(encode_graph([value]))[0]
        row = value.sections["measurements"]["first"]["signal"]
    before = compute_arg_hash([value])
    row.value = 99
    assert compute_arg_hash([value]) != before


def test_different_unpublished_dataset_rows_do_not_reuse_the_same_task():
    from simstack.core.node import compute_arg_hash

    values = []
    for signal in (1, 99):
        section = DataSetSection()
        section.add_row({"signal": FloatData(value=signal)}, name="row")
        values.append(DataSet(metadata=DataSetMetadata(field_name="experiment"), sections={"measurements": section}))
    assert values[0].sections["measurements"].data == values[1].sections["measurements"].data == {}
    assert compute_arg_hash([values[0]]) != compute_arg_hash([values[1]])


@pytest.mark.parametrize("array", [False, True])
def test_materialized_scientific_list_members_affect_node_hash(array):
    from simstack.core.node import compute_arg_hash
    from simstack.models.array_storage import ArrayStorage
    from simstack.models.base_lists import StringDataList
    import numpy as np

    if array:
        member = ArrayStorage(name="signal")
        member.array = np.array([1, 2])
        value = ArrayList(elements=[member])
    else:
        member = StringData(value="before")
        value = StringDataList(elements=[member])
    before = compute_arg_hash([value])
    if array:
        member.array = np.array([99, 2])
    else:
        member.value = "after"
    assert compute_arg_hash([value]) != before


def test_transport_and_revision_state_do_not_change_scientific_member_hash():
    from simstack.core.node import compute_arg_hash
    from simstack.models.base_lists import StringDataList

    member = StringData(value="signal")
    value = StringDataList(elements=[member])
    before = compute_arg_hash([value])
    for item in (value, member):
        item._scientific_revision = "new-server-baseline"
        object.__setattr__(item, "_object_requests", object())
        object.__setattr__(item, "_execution_generation", "replacement")
        object.__setattr__(item, "_simstack_persisted_document", {"old": "snapshot"})
    assert compute_arg_hash([value]) == before


def test_direct_array_and_pandas_scientific_changes_still_invalidate_node_cache():
    from simstack.core.node import compute_arg_hash
    from simstack.models.array_storage import ArrayStorage
    from simstack.models.pandas_model import PandasModel
    import numpy as np
    import pandas as pd

    array = ArrayStorage(name="signal")
    array.array = np.array([1, 2])
    table = PandasModel.from_data_frame(pd.DataFrame({"signal": [1, 2]}))
    array_before, table_before = compute_arg_hash([array]), compute_arg_hash([table])
    array.array = np.array([99, 2])
    table.table = pd.DataFrame({"signal": [99, 2]})
    assert compute_arg_hash([array]) != array_before
    assert compute_arg_hash([table]) != table_before


def test_graph_preserves_shared_subtype_tuple_nullable_and_scalar_values():
    child = ScientificSubtype(value=1.5, unit="eV")
    source = ScientificEnvelope(first=child, second=child, pair=(child, "energy"), metadata={
        "bytes": b"\x00\xff", "decimal": Decimal("1.001"),
        "instant": datetime(2026, 11, 9, tzinfo=timezone.utc),
        "large": 2**60, "__simstack_value__": "user data",
        "literal": {"__simstack_type__": "date", "value": "user text"},
    })
    graph = json.loads(json.dumps(encode_graph([source])))
    restored = decode(graph)[0]
    assert isinstance(restored.first, ScientificSubtype)
    assert restored.first is restored.second
    assert restored.pair[0] is not restored.first
    assert restored.pair[0] == restored.first
    assert isinstance(restored.pair, tuple)
    assert restored.optional is None
    assert restored.metadata == source.metadata
    assert restored.first.unit == "eV"


def test_graph_rejects_missing_and_cyclic_values():
    child = ScientificSubtype(value=1, unit="eV")
    graph = encode_graph([child])
    graph["nodes"] = []
    with pytest.raises(ValueError, match="Missing scientific value"):
        decode(graph)
    graph = encode_graph([child])
    graph["nodes"][0]["value"]["value"] = {
        "__simstack_value__": "ref", **graph["roots"][0],
    }
    with pytest.raises(ValueError, match="Cyclic"):
        decode(graph)


def test_graph_keeps_last_explicit_value_for_mutable_shared_identity():
    value = ScientificSubtype(value=1, unit="eV")
    updated = value.model_copy(update={"unit": "Hartree"})
    first, second = decode(encode_graph([value, updated]))
    assert first is second
    assert first.unit == "Hartree"


def test_graph_keeps_custom_identity_aliases_and_explicit_field_serializers():
    class Measurement(Model):
        key: str = Field(primary_field=True)
        name: str = Field(key_name="display_name")
        value: int

        @field_serializer("value")
        def serialize_value(self, value):
            return value + 1

    source = Measurement(key="run-a", name="signal", value=2)
    graph = encode_graph([source])
    assert graph["nodes"][0]["value"] == {"id": "run-a", "display_name": "signal", "value": 3}
    restored = decode(graph)[0]
    assert restored.key == "run-a"
    assert restored.name == "signal"
    assert restored.value == 3


def test_graph_honors_excluded_fields_computed_values_and_model_serializer():
    class PublicationView(Model):
        child: ScientificBase = Reference()
        runtime_only: str = Field(default="private-local-state", exclude=True)

        @computed_field
        @property
        def doubled(self) -> float:
            return self.child.value * 2

        @model_serializer(mode="wrap")
        def scientific_view(self, handler):
            return {**handler(self), "scientific_format": "v1"}

    child = ScientificSubtype(value=3, unit="eV")
    source = PublicationView(child=child)
    graph = encode_graph([source])
    root = next(item for item in graph["nodes"] if item["id"] == str(source.id))
    assert "runtime_only" not in root["value"]
    assert root["value"]["doubled"] == 6
    assert root["value"]["scientific_format"] == "v1"
    assert root["value"]["child"] == {"__simstack_value__": "ref",
        "mapping": f"{ScientificSubtype.__module__}.{ScientificSubtype.__qualname__}", "id": str(child.id)}
    assert decode(graph)[0].child.unit == "eV"


def test_excluded_field_serializer_is_not_called_or_encoded():
    class LocalState(Model):
        value: int
        runtime_only: str = Field(default="private-local-state", exclude=True)

        @field_serializer("runtime_only")
        def local_serializer(self, value):
            raise AssertionError("Excluded local state must never be serialized")

    value = LocalState(value=3)
    assert encode_graph([value])["nodes"][0]["value"] == {"id": str(value.id), "value": 3}


@pytest.mark.parametrize("when_used", ["always", "json"])
def test_reference_field_serializer_keeps_the_child_record_and_subtype(when_used):
    class SerializedReferenceView(Model):
        child: ScientificBase = Reference()

        @field_serializer("child", when_used=when_used)
        def serialize_child(self, child):
            return str(child.id) if when_used == "json" else {**child.model_dump(mode="python"), "value": child.value * 2}

    source = SerializedReferenceView(child=ScientificSubtype(value=3, unit="eV"))
    graph = encode_graph([source])
    assert len(graph["nodes"]) == 2
    restored = decode(graph)[0]
    assert isinstance(restored.child, ScientificSubtype)
    assert restored.child.id == source.child.id and restored.child.unit == "eV"
    assert restored.child.value == 3


@pytest.mark.parametrize("identity", [b"\x00abc\xff", Decimal("1.25"),
    UUID("12345678-1234-5678-1234-567812345678"), datetime(2026, 11, 9, tzinfo=timezone.utc)])
def test_graph_preserves_typed_primary_identities(identity):
    class TypedIdentity(Model):
        key: bytes | Decimal | UUID | datetime = Field(primary_field=True)
        value: int

    source = TypedIdentity(key=identity, value=3)
    graph = json.loads(json.dumps(encode_graph([source])))
    restored = decode(graph)[0]
    assert type(restored.id) is type(identity)
    assert restored.id == identity and restored.value == 3


@pytest.mark.parametrize("alias", [None, "display_double"])
def test_strict_model_recomputes_serialized_computed_fields_on_read(alias):
    class StrictScientificView(Model):
        model_config = {"extra": "forbid"}
        child: ScientificBase = Reference()
        runtime_only: str = Field(default="local-default", exclude=True)

        @computed_field(alias=alias)
        @property
        def doubled(self) -> float:
            return self.child.value * 2

    source = StrictScientificView(child=ScientificSubtype(value=3, unit="eV"))
    graph = encode_graph([source])
    root = next(item for item in graph["nodes"] if item["id"] == str(source.id))
    assert root["value"][alias or "doubled"] == 6
    assert "runtime_only" not in root["value"]
    restored = decode(graph)[0]
    assert isinstance(restored.child, ScientificSubtype)
    assert restored.child.unit == "eV" and restored.doubled == 6
    assert restored.runtime_only == "local-default"


@pytest.mark.asyncio
async def test_dataset_and_tuple_publication_build_locally_and_roundtrip():
    item = FloatData(value=3.0)
    section = DataSetSection()
    section.add_row({"energy": item}, name="conformer-a")
    await section.prepare_publication()
    dataset = DataSet(field_name="energies", metadata=DataSetMetadata(field_name="energies-meta"),
                      sections={"results": section})
    restored = decode(encode_graph([dataset]))[0]
    assert restored["results"].get_item("conformer-a")["energy"].value == 3
    assert restored["results"].data == {"conformer-a": {"energy": item.id}}
    tuples = DataSetTupleSection()
    await tuples.append((item, StringData(value="a")))
    await tuples.prepare_publication()
    tuple_dataset = DataSetTuple(metadata=dataset.metadata, sections={"results": tuples})
    restored_tuple = decode(encode_graph([tuple_dataset]))[0]
    assert restored_tuple["results"][0][0].value == 3
    assert list(restored_tuple["results"])[0][1].value == "a"
    cloned = await restored_tuple.clone("cloned-results")
    assert cloned["results"][0][0].id == item.id
    await cloned["results"].append((FloatData(value=4), StringData(value="b")))
    assert len(cloned["results"]) == 2
    assert len(restored_tuple["results"]) == 1
    assert not hasattr(dataset, "save")
    assert not hasattr(section, "db_find_postprocess")


@pytest.mark.asyncio
async def test_selection_iteration_only_uses_explicitly_materialized_dataset():
    item = FloatData(value=3)
    section = DataSetSection()
    section.add_row({"energy": item}, "row-a")
    await section.prepare_publication()
    dataset = DataSet(metadata=DataSetMetadata(field_name="test"), sections={"results": section})
    selection = DataSetSelection(dataset_id=dataset.id,
        dataset_selection_fields=[DataSetSelectionField(section_name="results", indices=[0])])
    with pytest.raises(ValueError, match="explicitly"):
        await selection.get_dataset()
    selection.materialize(dataset)
    restored = decode(encode_graph([selection]))[0]
    assert [row async for row in restored] == [(item,)]


def test_metadata_template_validation_is_pure_and_checks_types_and_freeze():
    metadata = DataSetMetadata(field_name="energies", data={"method": "DFTB"})
    reference, valid = validate_metadata_template(metadata, None, {"results": {"energy": "FloatData"}})
    assert valid
    original = reference.model_copy(deep=True)
    updated, valid = validate_metadata_template(metadata, reference, {
        "results": {"energy": "FloatData", "label": "StringData"},
    })
    assert valid and updated.structure["results"]["label"] == "StringData"
    assert reference == original
    with pytest.raises(ValueError, match="different value"):
        validate_metadata_template(metadata, reference, {"results": {"energy": "StringData"}})


class RecordingRequests:
    authorization = "runner"
    object_api_prefix = "/runner/objects"

    def __init__(self):
        self.calls = []

    def revision(self):
        return "revision-a"

    async def command(self, path, body):
        self.calls.append((path, body))
        return {"graph": body["graph"]}


@pytest.mark.asyncio
async def test_publication_does_not_upload_or_materialize_excluded_runtime_values(monkeypatch):
    class RuntimeValues(Model):
        value: int
        runtime_file: FileStack = Field(default_factory=lambda: FileStack(name="runtime-only.txt"), exclude=True)
        runtime_array: ArrayList = Field(default_factory=lambda: ArrayList(elements=[ObjectId()]), exclude=True)

    def unexpected_upload(*args, **kwargs):
        raise AssertionError("Excluded runtime file must not be uploaded")
    monkeypatch.setattr(FileStack, "ensure_uploaded", unexpected_upload)
    requests = RecordingRequests()
    source = RuntimeValues(value=3)
    result = await ScientificValuesAPI(requests).publish_value(source)
    assert result.value == 3
    assert len(requests.calls) == 1
    path, body = requests.calls[0]
    assert path == "/values/publications"
    assert len(body["graph"]["nodes"]) == 1
    assert body["graph"]["nodes"][0]["value"] == {"id": str(source.id), "value": 3}
    assert "_cache" not in vars(result.runtime_array)


@pytest.mark.asyncio
async def test_publication_is_explicit_and_graph_codec_has_no_upload_side_effect(monkeypatch):
    calls = []
    stack = FileStack(name="output.dat")
    monkeypatch.setattr(FileStack, "ensure_uploaded", lambda value, **kwargs: calls.append(value.id))
    files = FileListModel(elements=[stack])
    graph = encode_graph([files])
    assert not calls
    assert decode(graph)[0][0].id == stack.id
    requests = RecordingRequests()
    client = ScientificValuesAPI(requests)
    restored = await client.publish_value(files)
    assert calls == [stack.id]
    assert restored[0].id == stack.id
    path, body = requests.calls[0]
    assert path == "/values/publications"
    assert body["repository_revision"] == "revision-a"
    assert "collection" not in json.dumps(body)
    assert "changed_fields" not in json.dumps(body)


@pytest.mark.asyncio
async def test_catalogue_discovery_needs_no_context_or_storage(tmp_path):
    from simstack.tables.catalogue import CatalogueCollector
    from simstack.tables.model_table import CreateModelTable

    module = types.ModuleType("scientific_catalogue_fixture")
    exec("from simstack.models.document import Model\nclass Record(Model):\n    unit: str\n", module.__dict__)
    collector = CatalogueCollector()
    builder = CreateModelTable(collector, project_root=tmp_path)
    await builder._process_module(module, "")
    descriptor = collector.model_by_name("Record")
    assert descriptor.mapping == "scientific_catalogue_fixture.Record"
    assert "unit" in json.loads(descriptor.json_schema)["properties"]


@pytest.mark.parametrize("snapshot_first", [False, True])
def test_graph_keeps_independent_snapshot_beside_reference(snapshot_first):
    class SnapshotFirstEnvelope(Model):
        pair: tuple[ScientificBase, str]
        first: ScientificBase = Reference()
        second: ScientificBase = Reference()

    current = ScientificSubtype(value=2, unit="eV")
    historical = current.model_copy(update={"value": 1})
    model = SnapshotFirstEnvelope if snapshot_first else ScientificEnvelope
    source = model(first=current, second=current, pair=(historical, "energy"))
    restored = decode(encode_graph([source]))[0]
    assert restored.first is restored.second
    assert restored.first.value == 2
    assert restored.pair[0].value == 1
    assert restored.pair[0] is not restored.first


def test_nested_filestack_hash_uses_bytes_and_ignores_staging_and_transport(tmp_path):
    from simstack.core.node import compute_arg_hash
    from simstack.models.file_instance import FileInstance

    class InputWithFile(Model):
        hsd_file: FileStack

    first = FileStack.from_string("same scientific input", "input.hsd")
    second = FileStack.from_string("same scientific input", "input.hsd")
    before = compute_arg_hash([InputWithFile(hsd_file=first)])
    assert compute_arg_hash([InputWithFile(hsd_file=second)]) == before
    first.locations = [FileInstance(path="key", resource="__s3__", created_at=datetime.now(),
        location_type="s3", bucket="files", object_key="key")]
    assert compute_arg_hash([InputWithFile(hsd_file=first)]) == before
    different = FileStack.from_string("changed scientific input", "input.hsd")
    assert compute_arg_hash([InputWithFile(hsd_file=different)]) != before
