from enum import Enum
from typing import Any, Dict, List

from odmantic import Model, Field
from pydantic import field_validator

from simstack.models import simstack_model


class SimpleTableColumnType(str, Enum):
    STRING = "string"
    NUMBER = "number"

    @classmethod
    def from_value(
        cls, value: "SimpleTableColumnType | str"
    ) -> "SimpleTableColumnType":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError(f"Invalid SimpleTable column type: {value!r}")

        normalized = value.strip()
        try:
            return cls(normalized)
        except ValueError:
            pass

        try:
            return SIMPLE_TABLE_COLUMN_TYPE_LEGACY_ALIASES[normalized.lower()]
        except KeyError as exc:
            raise ValueError(f"Invalid SimpleTable column type: {value!r}") from exc


SIMPLE_TABLE_COLUMN_TYPE_LEGACY_ALIASES = {
    # Existing producers and stored DB documents used these variants before
    # SimpleTable column types were narrowed to logical data types.
    "str": SimpleTableColumnType.STRING,
    "int": SimpleTableColumnType.NUMBER,
    "float": SimpleTableColumnType.NUMBER,
}


@simstack_model
class SimpleTable(Model):
    """
    A simple table model to display tabular data using ag-grid
    """

    name: str = Field(default="SimpleTable")
    heading: List[str] = Field(default_factory=list)
    row: List[Dict[str, Any]] = Field(default_factory=list)
    type: List[SimpleTableColumnType] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_column_types(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, list):
            return [SimpleTableColumnType.from_value(item) for item in value]
        return value

    def add_column(
        self, column_name: str, column_type: SimpleTableColumnType | str
    ) -> None:
        if column_name not in self.heading:
            self.heading.append(column_name)
            self.type.append(SimpleTableColumnType.from_value(column_type))

    def add_row(self, row: Dict[str, Any]) -> None:
        self.row.append(row)

    @classmethod
    def ui_schema(cls) -> Dict[str, str]:
        ui_schema = {
            "ui:field": "SimpleTableField",
        }
        return ui_schema
