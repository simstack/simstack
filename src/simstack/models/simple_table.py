from typing import Dict, Any, List

from odmantic import Model, Field

from simstack.models import simstack_model


@simstack_model
class SimpleTable(Model):
    """
    A simple table model to display tabular data using ag-grid
    """

    name: str = Field(default="SimpleTable")
    heading: List[str] = Field(default_factory=list)
    row: List[Dict[str, Any]] = Field(default_factory=list)
    type: List[str] = Field(default_factory=list)

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
