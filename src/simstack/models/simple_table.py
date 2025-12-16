from typing import Dict, Any, List

from odmantic import Model, Field
from pydantic import model_validator

from simstack.models import simstack_model


@simstack_model
class SimpleTable(Model):
    """
    A simple table model to display tabular data using ag-grid
    """

    field_name: str = "SimpleTable"
    name: str = Field(default="SimpleTable")
    heading: List[str] = Field(default_factory=list)
    row: List[Dict[str, Any]] = Field(default_factory=list)
    type: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def ensure_fieldname(cls, data):
        """Ensure fieldname is set for existing documents"""
        if isinstance(data, dict) and "field_name" not in data:
            data["field_name"] = cls.__name__
        return data

    def add_column(self, column_name: str, column_type: str):
        if column_name not in self.heading:
            self.heading.append(column_name)
            self.type.append(column_type)

    def add_row(self, row: Dict[str, Any]):
        self.row.append(row)

    @classmethod
    def ui_schema(cls) -> dict:
        ui_schema = {
            "ui:field": "SimpleTableField",
        }
        return ui_schema
