import json
import base64
import zlib
from typing import Optional

from odmantic import Model
from pydantic import model_validator

from simstack.models.simstack_model import simstack_model
from simstack.util.ui_tools import ui_hide_fields
from simstack.util.b64mixin import BytesB64Mixin


@simstack_model
class ArrayStorage(BytesB64Mixin, Model):
    name: Optional[str]
    shape: Optional[str] = None  # Store array shape as string like "3,3"
    field_name: Optional[str] = None  # Store flattened array data as compressed JSON
    data_json: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def copy_name_to_field_name(cls, values):
        if isinstance(values, dict) and 'name' in values and 'field_name' not in values:
            values['field_name'] = values['name']
        return values

    def set_array(self, array):
        """Store a numpy array"""
        self.shape = ",".join(str(dim) for dim in array.shape)
        data_str = json.dumps(array.flatten().tolist())
        self.data_json = self._compress_bytes(data_str.encode())


    def get_array(self):
        """Retrieve the numpy array"""
        import numpy as np

        shape = tuple(int(dim) for dim in self.shape.split(",")) if self.shape else ()
        if self.data_json:
            data_str = self._decompress_bytes(self.data_json).decode()
            flat_array = np.array(json.loads(data_str))
        else:
            flat_array = np.array([])
        return flat_array.reshape(shape)

    @property
    def array(self):
        """Property getter for array"""
        return self.get_array()

    @array.setter
    def array(self, value):
        """Property setter for array"""
        self.set_array(value)

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
        return [{"field": "name", "headerName": "Array"}]

    @classmethod
    def ui_schema(cls, **kwargs) -> dict:
        return ui_hide_fields({}, ["shape", "field_name"])
