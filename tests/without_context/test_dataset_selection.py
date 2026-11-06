"""Selections resolve stored row positions through the existing dataset cache."""

import importlib
from types import SimpleNamespace

import pytest

from simstack.models import StringData
from simstack.models.dataset import (
    DataSet,
    DataSetSection,
    DataSetSelection,
    DataSetSelectionField,
)
from simstack.models.dataset_metadata import DataSetMetadata
from simstack.models.document import ObjectId
from simstack.models.models import ModelMapping
from simstack.persistence.memory import InMemoryRepository
from simstack.util.db import Database
from simstack.util.mappings import ModelMappingTable


@pytest.fixture
def database(monkeypatch):
    db = Database(repository=InMemoryRepository())
    mapping = ModelMapping(
        name="StringData",
        mapping="simstack.models.base_types.StringData",
        collection_name="string_data",
    )
    runtime = SimpleNamespace(db=db, model_mappings=ModelMappingTable([mapping]))
    context_module = importlib.import_module("simstack.core.context")
    importer = importlib.import_module("simstack.util.importer")
    monkeypatch.setattr(context_module, "context", runtime)
    monkeypatch.setattr(importer, "context", runtime)
    return db


async def stored_dataset(database):
    section = DataSetSection(
        model_types={
            "left": "StringData",
            "right": "StringData",
            "optional": "StringData",
        }
    )
    for index, row_name in enumerate(("row-z", "row-a", "row-middle")):
        row = {
            "right": StringData(value=f"right-{index}"),
            "left": StringData(value=f"left-{index}"),
        }
        if index == 1:
            row["optional"] = StringData(value="optional-1")
        section.add_row(row, name=row_name)
    auxiliary = DataSetSection()
    auxiliary.add_row({"value": StringData(value="auxiliary")}, name="only-row")
    dataset = DataSet(
        metadata=DataSetMetadata(field_name="selection-regression"),
        sections={"main": section, "auxiliary": auxiliary},
    )
    await database.save(dataset)
    return dataset


def selection(dataset, indices, section_name="main"):
    return DataSetSelection(
        dataset_id=dataset.id,
        dataset_selection_fields=[
            DataSetSelectionField(section_name=section_name, indices=indices)
        ],
    )


async def collect(value, method, section_name=None):
    if method == "getter":
        return value.get_selected_elements(section_name=section_name)
    return [row async for row in value.__aiter__(section_name=section_name)]


def values(rows):
    return [tuple(model.value if model is not None else None for model in row) for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["getter", "iterator"])
async def test_selection_preserves_stored_rows_columns_duplicates_and_optional_cells(database, method):
    dataset = await stored_dataset(database)
    before = dataset.model_dump_doc()
    chosen = selection(dataset, [2, 0, 2, 1])

    rows = await collect(chosen, method)

    assert values(rows) == [
        ("left-2", "right-2", None),
        ("left-0", "right-0", None),
        ("left-2", "right-2", None),
        ("left-1", "right-1", "optional-1"),
    ]
    assert rows[0][0].id != rows[0][1].id
    assert rows[0][0].id == rows[2][0].id
    persisted = await database.find_one(DataSet, DataSet.id == dataset.id)
    assert persisted.model_dump_doc() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["getter", "iterator"])
async def test_selection_preserves_sections_and_supports_section_filter(database, method):
    dataset = await stored_dataset(database)
    chosen = selection(dataset, [1])
    chosen.dataset_selection_fields.insert(
        0, DataSetSelectionField(section_name="auxiliary", indices=[0])
    )

    assert values(await collect(chosen, method)) == [
        ("auxiliary",),
        ("left-1", "right-1", "optional-1"),
    ]
    assert values(await collect(chosen, method, section_name="main")) == [
        ("left-1", "right-1", "optional-1")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["getter", "iterator"])
@pytest.mark.parametrize("empty_fields", [False, True])
async def test_empty_selection_has_no_rows(database, method, empty_fields):
    dataset = await stored_dataset(database)
    chosen = selection(dataset, [])
    if empty_fields:
        chosen.dataset_selection_fields = []

    assert await collect(chosen, method) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["getter", "iterator"])
@pytest.mark.parametrize("index", [-1, 3])
async def test_selection_rejects_invalid_stored_row_positions(database, method, index):
    dataset = await stored_dataset(database)

    with pytest.raises(IndexError, match="out of range"):
        await collect(selection(dataset, [index]), method)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["getter", "iterator"])
async def test_selection_rejects_missing_dataset_and_section(database, method):
    dataset = await stored_dataset(database)
    missing = selection(dataset, [0])
    missing.dataset_id = ObjectId()
    with pytest.raises(ValueError, match="Dataset with id .* not found"):
        await collect(missing, method)
    with pytest.raises(ValueError, match="Section missing not found"):
        await collect(selection(dataset, [0], section_name="missing"), method)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["getter", "iterator"])
async def test_selection_fails_when_stored_row_cannot_be_loaded_to_cache(database, method):
    dataset = await stored_dataset(database)
    await database.delete(dataset.sections["main"].get_item("row-z")["left"])

    with pytest.raises(KeyError, match="row-z.*not found"):
        await collect(selection(dataset, [0]), method)
