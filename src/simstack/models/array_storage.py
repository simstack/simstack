import json
import zlib
import io
from typing import ClassVar, Optional

from simstack.models.document import Model, Reference
from simstack.models.files import FileStack
from pydantic import model_validator

from simstack.models.simstack_model import simstack_model
from simstack.util.ui_tools import ui_hide_fields
from simstack.util.b64mixin import BytesB64Mixin


@simstack_model
class ArrayStorage(BytesB64Mixin, Model):
    __payload_fields__: ClassVar[tuple[dict, ...]] = ({
        "source": "data_json", "target": "payload_file", "codec": "npy", "codec_version": 1,
    },)
    name: Optional[str]
    shape: Optional[str] = None  # Store array shape as string like "3,3"
    field_name: Optional[str] = None  # Store flattened array data as compressed JSON
    data_json: Optional[str] = None
    payload_file: Optional[FileStack] = Reference(None)
    dtype: Optional[str] = None
    payload_format: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def copy_name_to_field_name(cls, values):
        if isinstance(values, dict) and 'name' in values and 'field_name' not in values:
            values['field_name'] = values['name']
        return values

    def set_array(self, array):
        """Store a numpy array"""
        if array.dtype.hasobject:
            raise ValueError("ArrayStorage object dtype cannot be stored as safe NPY")
        self.shape = ",".join(str(dim) for dim in array.shape)
        data_str = json.dumps(array.flatten().tolist())
        self.data_json = self._compress_bytes(data_str.encode())
        self.payload_file = None
        self.payload_format = None
        self.dtype = array.dtype.str

    def prepare_blob_payload(self) -> None:
        """Convert legacy JSON to a dtype-preserving NPY file explicitly."""
        if self.payload_file is not None:
            return
        import numpy as np

        array = self.get_array()
        if array.dtype.hasobject:
            raise ValueError("ArrayStorage object dtype cannot be stored as safe NPY")
        output = io.BytesIO()
        np.save(output, array, allow_pickle=False)
        self.payload_file = FileStack.from_bytes(output.getvalue(), "array.npy")
        self.payload_format = "npy"
        self.dtype = array.dtype.str
        self.data_json = None


    def get_array(self):
        """Retrieve the numpy array"""
        import numpy as np

        if self.payload_file is not None:
            if self.payload_format != "npy":
                raise ValueError("Unsupported ArrayStorage payload format")
            array = np.load(io.BytesIO(self.payload_file.read_bytes()), allow_pickle=False)
            shape = tuple(int(dim) for dim in self.shape.split(",")) if self.shape else ()
            if array.shape != shape or (self.dtype is not None and array.dtype.str != self.dtype):
                raise ValueError("ArrayStorage payload does not match shape/dtype metadata")
            return array

        shape = tuple(int(dim) for dim in self.shape.split(",")) if self.shape else ()
        flat_array = np.array([])
        if self.data_json:
            try:
                # Try to decompress assuming it's compressed
                data_str = self._decompress_bytes(self.data_json).decode()
            except (zlib.error, Exception):
                # If decompression fails, treat as uncompressed
                data_str = self.data_json
            flat_array = np.array(json.loads(data_str), dtype=self.dtype)
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
