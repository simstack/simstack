import json
from typing import Optional

from odmantic import Model

from simstack.models.simstack_model import simstack_model
from simstack.util.ui_tools import ui_hide_fields


@simstack_model
class ArrayStorage(Model):
    name: str
    shape: Optional[str] = None  # Store array shape as string like "3,3"
    data_json: Optional[str] = None  # Store flattened array data as JSON

    def set_array(self, array):
        """Store a numpy array"""
        self.shape = ",".join(str(dim) for dim in array.shape)
        self.data_json = json.dumps(array.flatten().tolist())

    def get_array(self):
        """Retrieve the numpy array"""
        import numpy as np

        shape = tuple(int(dim) for dim in self.shape.split(",")) if self.shape else ()
        flat_array = (
            np.array(json.loads(self.data_json)) if self.data_json else np.array([])
        )
        return flat_array.reshape(shape)

    def make_table_entries(
        self,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return {"name": self.name}

    def make_column_defs_instance(
        self,
        table_name=None,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return [{"field": self.name, "headerName": "Array"}]

    @classmethod
    def ui_schema(cls, **kwargs) -> dict:
        return ui_hide_fields({}, ["shape", "data_json"])
