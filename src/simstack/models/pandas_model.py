import asyncio
import json
import io
from enum import Enum
from pathlib import Path
from pprint import pprint
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from odmantic import Model
from pydantic import model_validator

from simstack.core.context import context
from simstack.models import simstack_model
from simstack.models.files import FileStack, MONGODB_MAX_DOCUMENT_SIZE, logger
from simstack.util.b64mixin import BytesB64Mixin


class StorageModeEnum(str, Enum):
    IN_MEMORY = "in_memory"
    FILE = "file"
    AUTO = "auto"


def _format_df_for_console(df: pd.DataFrame, precision: int = 2, max_rows: int = 60, max_cols: int = 30) -> str:
    """
    Console-safe DataFrame formatting (no optional deps like jinja2 required).
    """
    if df is None or df.empty:
        return "Empty DataFrame"

    df_out = df.copy()

    # Round only numeric columns to avoid touching strings/datetimes
    numeric_cols = df_out.select_dtypes(include=["number"]).columns
    if len(numeric_cols) > 0:
        df_out[numeric_cols] = df_out[numeric_cols].round(precision)

    with pd.option_context(
        "display.max_rows", max_rows,
        "display.max_columns", max_cols,
        "display.width", 0,  # auto-detect width
        "display.max_colwidth", 200,
    ):
        return df_out.to_string(index=False)


def _format_datetime_columns(df):
    """
    Format all datetime columns to match the expected format: YYYY-MM-DDThh:mm:ss
    Returns a copy of the dataframe with formatted datetime columns.
    """
    df_copy = df.copy()
    # Format datetime columns to match expected format without microseconds and Z
    for col in df_copy.select_dtypes(include=["datetime64"]):
        df_copy[col] = df_copy[col].dt.strftime("%Y-%m-%dT%H:%M:%S")
    return df_copy


@simstack_model
class PandasModel(BytesB64Mixin, Model):
    model_config = {"indexes": [("field_name", {"unique": True})]}

    field_name: str = "pandas_model"
    storage_mode: StorageModeEnum = StorageModeEnum.AUTO
    content_: Optional[str] = None
    file_stack: Optional[FileStack] = None

    @model_validator(mode="before")
    @classmethod
    def copy_name_to_field_name(cls, values):
        if isinstance(values, dict) and "name" in values and "field_name" not in values:
            values["field_name"] = values["name"]
        return values

    def _get_next_filename(self):
        if not self.field_name:
            base = "pandas_model"
        else:
            base = self.field_name

        i = 0
        while True:
            filename = f"{base}.{i}.pkl"
            if not Path(filename).exists():
                return filename
            i += 1

    @classmethod
    def from_data_frame(cls, df):
        new_instance = cls()
        new_instance.table = df
        return new_instance

    @property
    def table(self):
        mode = self.storage_mode
        if mode == StorageModeEnum.AUTO:
            if not self.content_ and self.file_stack is None:
                return pd.DataFrame()
            # If it was saved with AUTO somehow, we need to decide. 
            # But usually it's set to IN_MEMORY or FILE during setter.
            if self.file_stack:
                mode = StorageModeEnum.FILE
            else:
                mode = StorageModeEnum.IN_MEMORY

        if mode == StorageModeEnum.FILE:
            if self.file_stack is None:
                raise ValueError(f"File stack not set for pandas storage: {self.field_name}")
            local_file = self.file_stack.get()
            return pd.read_pickle(local_file)

        if not self.content_:
            return pd.DataFrame()

        # Create a BytesIO object from the binary content
        try:
            # Try to decompress assuming it's compressed
            data = self._decompress_bytes(self.content_)
        except Exception:
            # If decompression fails, treat as uncompressed
            data = self.content_

        buffer = io.BytesIO(data)

        # Use pandas read_pickle to decompress and load the DataFrame
        return pd.read_pickle(buffer)

    @table.setter
    def table(self, df):
        if not isinstance(df, pd.DataFrame):
            raise TypeError("Expected a pandas DataFrame")

        # Create a BytesIO object to store the binary content
        buffer = io.BytesIO()

        # Serialize the DataFrame to the buffer
        df.to_pickle(buffer)

        # Get the binary content from the buffer
        uncompressed_data = buffer.getvalue()

        mode = self.storage_mode
        if mode == StorageModeEnum.AUTO or mode == StorageModeEnum.IN_MEMORY:
            compressed_data = self._compress_bytes(uncompressed_data)
            if len(compressed_data) < 0.9 * MONGODB_MAX_DOCUMENT_SIZE:
                self.storage_mode = StorageModeEnum.IN_MEMORY
                self.content_ = compressed_data
                # We still might want to save to file as backup or for consistency with ArrayStorage?
                # ArrayStorage DOES save to file even in IN_MEMORY mode.
            else:
                logger.warning(
                    f"Compressed DataFrame size {len(compressed_data)} bytes exceeds MongoDB limit of {MONGODB_MAX_DOCUMENT_SIZE} bytes for {self.field_name}"
                )
                self.storage_mode = StorageModeEnum.FILE

        filename = self._get_next_filename()
        df.to_pickle(filename)
        self.file_stack = FileStack.from_local_file(
            filename, in_memory=False, secure_source=True
        )

    def to_react_json(self, orient="records"):
        """
        Convert the DataFrame to a JSON string suitable for React visualization libraries.

        Parameters:
        - orient: Determines the JSON string layout:
          'records' - list like [{column -> value}, ... , {column -> value}] (default)
          'columns' - {column -> [values, ...]}
          'index'   - {column -> value}}
          'split'   - {index -> [index], columns -> [columns], data -> [values]}
          'table'   - {'schema': {schema}, 'data': {data}}

        Returns:
        - String: JSON formatted string ready for React
        """
        df = self.table
        if df.empty:
            return json.dumps([])

        # Format datetime columns
        df = _format_datetime_columns(df)

        # Create a custom serializer to handle NumPy types properly
        class NumpyEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, np.integer):
                    return int(obj)  # Keep integers as integers
                elif isinstance(obj, np.floating):
                    return float(obj)  # Convert numpy float to Python float
                elif isinstance(obj, np.ndarray):
                    return obj.tolist()  # Convert arrays to lists
                elif pd.isna(obj):
                    return None  # Convert NaN/NaT to None
                return super().default(obj)

        # Convert DataFrame to dictionary while preserving data types
        if orient == "records":
            data = df.to_dict(orient="records")
        else:
            data = df.to_dict(orient=orient)

        # Use custom JSON encoder to handle NumPy types properly
        return json.dumps(data, cls=NumpyEncoder)

    def to_react_data(self, orient="records"):
        """
        Convert the DataFrame to a Python object suitable for conversion to JSON.
        This can be used in API responses.

        Returns:
        - List/Dict: Python object ready for json.dumps()
        """
        df = self.table
        if df.empty:
            return []

        # Format datetime columns using the same helper method
        df = _format_datetime_columns(df)

        # Convert DataFrame to dictionary while preserving data types
        if orient == "records":
            data = df.to_dict(orient="records")
        else:
            data = df.to_dict(orient=orient)

        # Replace NaN values with None
        if isinstance(data, list):
            for item in data:
                for key, value in item.items():
                    if pd.isna(value):
                        item[key] = None
        elif isinstance(data, dict):
            for key, values in data.items():
                if isinstance(values, dict):
                    for sub_key, value in values.items():
                        if pd.isna(value):
                            values[sub_key] = None
                elif isinstance(values, list):
                    data[key] = [None if pd.isna(v) else v for v in values]

        return data

    async def custom_model_dump(self, **kwargs) -> Dict[str, Any]:
        dumped_data = self.to_react_data("dict")
        # del dumped_data["content_"]  # Exclude content from the dumped data
        return dumped_data

    def __repr__(self):
        if not self.content_ and self.file_stack is None:
            return "PandasModel(empty table)"

        df = self.table
        rows, cols = df.shape
        return f"PandasModel({rows} rows × {cols} columns)"

    def __str__(self):
        if not self.content_ and self.file_stack is None:
            return "Empty pandas table"

        df = self.table
        if len(df) > 5:
            return f"PandasModel with shape {df.shape}:\n{df.head(5).to_string()}\n..."
        return f"PandasModel with shape {df.shape}:\n{df.to_string()}"

