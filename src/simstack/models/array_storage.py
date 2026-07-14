import json
import zlib
from enum import Enum
from pathlib import Path
from typing import Optional

from odmantic import Model
from pydantic import model_validator

from simstack.models.files import FileStack, MONGODB_MAX_DOCUMENT_SIZE, logger
from simstack.models.simstack_model import simstack_model
from simstack.util.ui_tools import ui_hide_fields
from simstack.util.b64mixin import BytesB64Mixin

class StorageModeEnum(str, Enum):
    IN_MEMORY = "in_memory"
    FILE = "file"
    AUTO = "auto"

@simstack_model
class ArrayStorage(BytesB64Mixin, Model):
    field_name: Optional[str] = None  # Store flattened array data as compressed JSON
    storage_mode: StorageModeEnum = StorageModeEnum.AUTO
    shape: Optional[str] = None  # Store array shape as string like "3,3"
    data_json: Optional[str] = None
    file_stack: Optional[FileStack] = None

    @model_validator(mode='before')
    @classmethod
    def copy_name_to_field_name(cls, values):
        if isinstance(values, dict) and 'name' in values and 'field_name' not in values:
            values['field_name'] = values['name']
        return values

    def _get_next_filename(self):
        if not self.field_name:
            base = "array"
        else:
            base = self.field_name
        
        i = 0
        while True:
            filename = f"{base}.{i}.npy"
            if not Path(filename).exists():
                return filename
            i += 1

    def set_array(self, array):
        """Store a numpy array"""
        import numpy as np
        self.shape = ",".join(str(dim) for dim in array.shape)
        
        mode = self.storage_mode
        if mode == StorageModeEnum.AUTO or mode == StorageModeEnum.IN_MEMORY:
            data_str = json.dumps(array.flatten().tolist())
            compressed_data_str = self._compress_bytes(data_str.encode())
            if len(compressed_data_str) < 0.9*MONGODB_MAX_DOCUMENT_SIZE:
                self.storage_mode = StorageModeEnum.IN_MEMORY
                self.data_json = compressed_data_str
                return
            else:
                logger.warning(f"Compressed array size {len(compressed_data_str)} bytes exceeds MongoDB limit of {MONGODB_MAX_DOCUMENT_SIZE} bytes for array {self.field_name}")
                self.storage_mode = StorageModeEnum.FILE

        filename = self._get_next_filename()
        np.save(filename, array)
        self.file_stack = FileStack.from_local_file(filename, in_memory=False, secure_source=True)

    def get_array(self):
        """Retrieve the numpy array"""
        import numpy as np
        shape = tuple(int(dim) for dim in self.shape.split(",")) if self.shape else ()
        
        mode = self.storage_mode
        if mode == StorageModeEnum.AUTO:
            raise ValueError(f"Storage mode not set for array storage: {self.field_name}")

        if mode == StorageModeEnum.FILE:
            if self.file_stack is None:
                raise ValueError(f"File stack not set for array storage: {self.field_name}")
            local_file = self.file_stack.get()
            np_array =  np.load(local_file)
            local_file.unlink()
            return np_array

        if self.data_json is None:
            raise ValueError(f"No data found for array storage: {self.field_name}")
        try:
            # Try to decompress assuming it's compressed
            data_str = self._decompress_bytes(self.data_json).decode()
        except (zlib.error, Exception):
            # If decompression fails, treat as uncompressed
            data_str = self.data_json
        flat_array = np.array(json.loads(data_str))
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
