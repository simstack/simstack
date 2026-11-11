"""Copied scientific helper identities follow their materialized records."""

import pytest

from simstack.methods.multiple_executor import MultipleExecutorInput, MultipleExecutorOutput
from simstack.models import FloatData, IntData
from simstack.models.dataset import DataSet, DataSetSection, DataSetSelection
from simstack.models.dataset_metadata import DataSetMetadata
from simstack.models.datasettuple import DataSetTuple, DataSetTupleSection, DataSetTupleSelection
from simstack.models.document import ObjectId
from simstack.server_api.scientific_value_graph import type_name
from simstack.util.model_copy import model_copy


def executor_input():
    return MultipleExecutorInput(
        function_mapping="fixture.unused", input_mappings=[type_name(IntData)],
        result_mapping=type_name(IntData),
    )


def helper_with_cache(kind):
    row = IntData(value=7)
    if kind in ("selection", "tuple_selection"):
        if kind == "selection":
            section = DataSetSection()
            section.add_row({"value": row}, name="row")
            dataset_type, selection_type = DataSet, DataSetSelection
        else:
            section = DataSetTupleSection()
            section.extend([(row,)])
            dataset_type, selection_type = DataSetTuple, DataSetTupleSelection
        dataset = dataset_type(metadata=DataSetMetadata(field_name="copy"), sections={"r": section})
        source = selection_type(dataset_id=dataset.id).materialize(dataset)
        return source, "dataset_id"
    if kind == "input":
        source = executor_input()
        identities = [row.id]
        object.__setattr__(source, "inputs_data", [identities, identities])
        cache = [row]
        object.__setattr__(source, "_cache", [cache, cache])
        return source, "inputs_data"
    source = MultipleExecutorOutput(executor_input=executor_input(),
        result_mapping=type_name(IntData), result_ids=[row.id, None, row.id])
    object.__setattr__(source, "_cache", [row, None, row])
    return source, "result_ids"


@pytest.mark.parametrize("kind", ["selection", "tuple_selection", "input", "output"])
@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize("preserve_ids", [False, True])
@pytest.mark.parametrize("copy_references", [False, True])
def test_helper_copy_aligns_cache_ids_without_breaking_aliases(kind, deep, preserve_ids, copy_references):
    source, field = helper_with_cache(kind)
    original_ids = getattr(source, field)
    object.__setattr__(source, "_cache_alias", source._cache)
    if field != "dataset_id":
        object.__setattr__(source, "_ids_alias", original_ids)
    copied = model_copy(source, deep=deep, preserve_ids=preserve_ids, copy_references=copy_references)
    cloned = deep and copy_references
    assert (copied.id == source.id) is preserve_ids
    assert copied._cache_alias is copied._cache
    if field == "dataset_id":
        original, member = source._cache, copied._cache
        assert copied.dataset_id == member.id
    elif field == "inputs_data":
        original, member = source._cache[0][0], copied._cache[0][0]
        assert copied._cache[0] is copied._cache[1]
        assert copied.inputs_data[0] is copied.inputs_data[1]
        assert copied.inputs_data == [[member.id], [member.id]]
    else:
        original, member = source._cache[0], copied._cache[0]
        assert copied._cache[2] is member
        assert copied._cache[1] is None
        assert copied.result_ids == [member.id, None, member.id]
    assert (member is not original) == cloned
    assert (member.id != original.id) == (cloned and not preserve_ids)
    if field != "dataset_id":
        assert copied._ids_alias is getattr(copied, field)
        assert (getattr(copied, field) is not original_ids) == deep
        member.value = 99
        assert original.value == (7 if cloned else 99)
    assert getattr(source, field) == original_ids
    if field == "dataset_id":
        assert source.dataset_id == original.id
    elif field == "inputs_data":
        assert source.inputs_data == [[original.id], [original.id]]
    else:
        assert source.result_ids == [original.id, None, original.id]


@pytest.mark.parametrize("kind", ["selection", "tuple_selection", "input", "output"])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("replaced", [False, True])
def test_helper_copy_preserves_unmaterialized_and_explicitly_replaced_ids(kind, cached, replaced):
    source, field = helper_with_cache(kind)
    explicit = ObjectId()
    if field == "dataset_id":
        original = source.dataset_id
        if replaced:
            source.dataset_id = explicit
    elif field == "inputs_data":
        original = source.inputs_data[0][0]
        source.inputs_data = [[explicit if replaced else original], [explicit]]
        object.__setattr__(source, "_cache", source._cache[:1])
    else:
        original = source.result_ids[0]
        source.result_ids = [explicit if replaced else original, None, explicit]
        object.__setattr__(source, "_cache", source._cache[:2])
    if not cached:
        del source.__dict__["_cache"]
    copied = model_copy(source, copy_references=True)
    if field == "dataset_id":
        expected = explicit if replaced else copied._cache.id if cached else original
        assert copied.dataset_id == expected
    elif field == "inputs_data":
        expected = explicit if replaced else copied._cache[0][0].id if cached else original
        assert copied.inputs_data == [[expected], [explicit]]
    else:
        expected = explicit if replaced else copied._cache[0].id if cached else original
        assert copied.result_ids == [expected, None, explicit]
    assert ("_cache" in vars(copied)) is cached


def test_executor_input_copy_qualifies_same_record_ids_by_column():
    identity = ObjectId()
    records = [IntData(id=identity, value=7), FloatData(id=identity, value=2.5)]
    source = executor_input()
    source.input_mappings = [type_name(type(record)) for record in records]
    source.inputs_data = [[identity, identity]]
    object.__setattr__(source, "_cache", [records])
    copied = model_copy(source, copy_references=True)
    assert copied._cache[0][0].id != copied._cache[0][1].id
    assert copied.inputs_data == [[record.id for record in copied._cache[0]]]
    assert source.inputs_data == [[identity, identity]]
