import asyncio
import io
import json
from pprint import pprint
from typing import Dict, Any

import numpy as np
import pandas as pd
from odmantic import Model

from simstack.core.context import context
from simstack.models import simstack_model


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
    content_: bytes = b""

    @classmethod
    def from_data_frame(cls, df):
        new_instance = cls()
        new_instance.table = df
        return new_instance

    @property
    def table(self):
        if not self.content_:
            return pd.DataFrame()

        # Create a BytesIO object from the binary content
        buffer = io.BytesIO(self.content_)

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
        self.content_ = buffer.getvalue()

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
        # del dumped_data["content"]  # Exclude content from the dumped data
        return dumped_data

    def __repr__(self):
        if not self.content_:
            return "PandasModel(empty table)"

        df = self.table
        rows, cols = df.shape
        return f"PandasModel({rows} rows × {cols} columns)"

    def __str__(self):
        if not self.content_:
            return "Empty pandas table"

        df = self.table
        if len(df) > 5:
            return f"PandasModel with shape {df.shape}:\n{df.head(5).to_string()}\n..."
        return f"PandasModel with shape {df.shape}:\n{df.to_string()}"


async def main():
    context.initialize()
    # Create the data structure
    data = []

    # Iteration names
    iterations = ["iter1", "iter2", "iter3"]

    # For each iteration
    for index, iteration in enumerate(iterations):
        # Generate 4 x values (for example, increasing by 0.5)
        x_values = np.arange(1, 3, 0.5)  # Creates [1.0, 1.5, 2.0, 2.5]

        # Generate 2 sets of 4 y values for each iteration
        y_values_set1 = np.sin((index + 1) * x_values)  # 4 random values around mean=8
        y_values_set2 = np.sin((index + 1) * x_values)
        # Add the data for this iteration
        for i, x in enumerate(x_values):
            data.append(
                {
                    "iteration": iteration,
                    "x": x,
                    "y_set1": y_values_set1[i],
                    "y_set2": y_values_set2[i],
                }
            )

    # Create the DataFrame
    df = pd.DataFrame(data)
    print(df.style.format(precision=2))

    model = PandasModel.from_data_frame(df)

    pprint(await model.custom_model_dump())
    saved_model = await context.db.save(model)

    retrieved_model = await context.db.find_one(
        PandasModel, PandasModel.id == saved_model.id
    )
    print("Retrieved Model", retrieved_model)


if __name__ == "__main__":
    asyncio.run(main())
