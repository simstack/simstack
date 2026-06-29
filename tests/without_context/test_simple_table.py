from bson import BSON
import pytest
from pydantic import ValidationError

from simstack.models.simple_table import SimpleTable, SimpleTableColumnType


def test_simple_table_column_type_accepts_existing_values():
    table = SimpleTable(
        type=[
            "float",
            "int",
            "number",
            "str",
            "string",
            SimpleTableColumnType.NUMBER,
        ]
    )

    assert table.type == [
        SimpleTableColumnType.NUMBER,
        SimpleTableColumnType.NUMBER,
        SimpleTableColumnType.NUMBER,
        SimpleTableColumnType.STRING,
        SimpleTableColumnType.STRING,
        SimpleTableColumnType.NUMBER,
    ]
    assert table.model_dump(mode="json")["type"] == [
        "number",
        "number",
        "number",
        "string",
        "string",
        "number",
    ]


def test_simple_table_add_column_normalizes_type():
    table = SimpleTable()

    table.add_column("energy", "float")
    table.add_column("label", "string")

    assert table.heading == ["energy", "label"]
    assert table.type == [SimpleTableColumnType.NUMBER, SimpleTableColumnType.STRING]


def test_simple_table_column_type_remains_string_in_bson():
    table = SimpleTable()
    table.add_column("energy", "float")
    doc = table.model_dump(by_alias=True)
    doc["_id"] = table.id

    decoded = BSON(BSON.encode(doc)).decode()

    assert decoded["type"] == ["number"]
    assert isinstance(decoded["type"][0], str)


def test_simple_table_rejects_unknown_column_type():
    with pytest.raises(ValidationError):
        SimpleTable(type=["not-a-table-column-type"])
