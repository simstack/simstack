import asyncio
import json
import io
from pprint import pprint
from typing import Any, Dict

import numpy as np
import pandas as pd
from odmantic import Model, Reference
from pydantic import model_validator

from simstack.core.context import context
from simstack.models import simstack_model
from simstack.models.files import FileStack


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
class PandasModel(Model):
    model_config = {"indexes": [("field_name", {"unique": True})]}

    field_name: str = "pandas_model"
    file_stack: FileStack = Reference()

    def __init__(self, **data):
        in_memory = data.pop("in_memory", True)
        data.setdefault("file_stack", FileStack(in_memory=in_memory))
        Model.__init__(self, **data)

    @model_validator(mode="before")
    @classmethod
    def copy_name_to_field_name(cls, values):
        if isinstance(values, dict) and "name" in values and "field_name" not in values:
            values["field_name"] = values["name"]
        return values

    @classmethod
    def from_data_frame(cls, df):
        new_instance = cls()
        new_instance.table = df
        return new_instance

    @property
    def table(self):
        if self.file_stack.content is None and not self.file_stack.locations:
            return pd.DataFrame()
        return pd.read_pickle(io.BytesIO(self.file_stack.get_bytes()))

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

        self.file_stack.set_bytes(
            uncompressed_data,
            "dataframe.pkl",
            in_memory=self.file_stack.in_memory,
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
        return dumped_data

    def __repr__(self):
        if self.file_stack.content is None and not self.file_stack.locations:
            return "PandasModel(empty table)"

        df = self.table
        rows, cols = df.shape
        return f"PandasModel({rows} rows × {cols} columns)"

    def __str__(self):
        if self.file_stack.content is None and not self.file_stack.locations:
            return "Empty pandas table"

        df = self.table
        if len(df) > 5:
            return f"PandasModel with shape {df.shape}:\n{df.head(5).to_string()}\n..."
        return f"PandasModel with shape {df.shape}:\n{df.to_string()}"
