import io
from typing import Optional

from odmantic import Model, Reference
from pydantic import model_validator

from simstack.models.files import FileStack
from simstack.models.simstack_model import simstack_model
from simstack.util.ui_tools import ui_hide_fields


@simstack_model
class ArrayStorage(Model):
    field_name: Optional[str] = None
    shape: Optional[str] = None  # Store array shape as string like "3,3"
    file_stack: FileStack = Reference()

    def __init__(self, **data):
        in_memory = data.pop("in_memory", True)
        data.setdefault("file_stack", FileStack(in_memory=in_memory))
        Model.__init__(self, **data)

    @model_validator(mode='before')
    @classmethod
    def copy_name_to_field_name(cls, values):
        if isinstance(values, dict) and 'name' in values and 'field_name' not in values:
            values['field_name'] = values['name']
        return values

    def set_array(self, array):
        """Store a numpy array"""
        import numpy as np

        buffer = io.BytesIO()
        np.save(buffer, array)
        self.shape = ",".join(str(dim) for dim in array.shape)
        self.file_stack.set_bytes(
            buffer.getvalue(),
            "array.npy",
            in_memory=self.file_stack.in_memory,
        )

    def get_array(self):
        """Retrieve the numpy array"""
        import numpy as np

        if self.file_stack.content is None and not self.file_stack.locations:
            raise ValueError(f"No data found for array storage: {self.field_name}")
        return np.load(io.BytesIO(self.file_stack.get_bytes()))

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
        return {"field_name": self.field_name}

    def make_column_defs_instance(
        self,
        table_name=None,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return [{"field": "field_name", "headerName": "Array"}]

    @classmethod
    def ui_schema(cls, **kwargs) -> dict:
        return ui_hide_fields({}, ["shape", "field_name"])
