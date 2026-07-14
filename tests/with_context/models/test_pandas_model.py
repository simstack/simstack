import pandas as pd
import numpy as np
import pytest
import os
from pathlib import Path
from simstack.models.pandas_model import PandasModel, StorageModeEnum
from simstack.core.context import context

@pytest.fixture
def tmp_workdir(tmp_path):
    """Fixture to create a temporary working directory and change to it."""
    old_cwd = os.getcwd()
    old_rd_workdir = context.config._resource_definition.workdir
    
    # Update context workdir to match tmp_path
    context.config._resource_definition.workdir = str(tmp_path)
    
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old_cwd)
        context.config._resource_definition.workdir = old_rd_workdir

@pytest.mark.asyncio
async def test_create_pandas_model(odmantic_engine, tmp_workdir):
    """Test creating and storing a new pandas model."""
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    model = PandasModel.from_data_frame(df)
    model.field_name = "test_df"

    await odmantic_engine.save(model)
    assert model.id is not None
    assert model.storage_mode == StorageModeEnum.IN_MEMORY

@pytest.mark.asyncio
async def test_retrieve_pandas_model(odmantic_engine, tmp_workdir):
    """Test retrieving a stored pandas model."""
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    model = PandasModel.from_data_frame(df)
    model.field_name = "retrieve_test"
    await odmantic_engine.save(model)

    retrieved = await odmantic_engine.find_one(PandasModel, PandasModel.id == model.id)
    assert retrieved is not None
    pd.testing.assert_frame_equal(df, retrieved.table)

@pytest.mark.asyncio
async def test_file_mode_pandas_model(odmantic_engine, tmp_workdir):
    """Test forcing and using FILE storage mode for pandas."""
    df = pd.DataFrame({"a": np.random.rand(10), "b": np.random.rand(10)})
    model = PandasModel(field_name="file_mode_test", storage_mode=StorageModeEnum.FILE)
    model.table = df

    assert model.storage_mode == StorageModeEnum.FILE
    assert model.file_stack is not None

    await odmantic_engine.save(model)

    retrieved = await odmantic_engine.find_one(PandasModel, PandasModel.id == model.id)
    assert retrieved.storage_mode == StorageModeEnum.FILE
    pd.testing.assert_frame_equal(df, retrieved.table)

@pytest.mark.asyncio
async def test_large_df_auto_fallback_to_file(odmantic_engine, tmp_workdir):
    """Test that large DataFrames automatically fall back to FILE mode."""
    # Create a large enough dataframe to exceed MONGODB_MAX_DOCUMENT_SIZE (16MB)
    # 1000000 rows, 2 columns of float64 should be ~16MB uncompressed.
    # We want it to be > 16MB even after compression.
    df = pd.DataFrame(np.random.rand(1000000, 2), columns=['a', 'b'])
    
    model = PandasModel(field_name="large_df", storage_mode=StorageModeEnum.AUTO)
    model.table = df

    assert model.storage_mode == StorageModeEnum.FILE
    assert model.file_stack is not None

    await odmantic_engine.save(model)

    retrieved = await odmantic_engine.find_one(PandasModel, PandasModel.id == model.id)
    assert retrieved.storage_mode == StorageModeEnum.FILE
    pd.testing.assert_frame_equal(df, retrieved.table)

@pytest.mark.asyncio
async def test_update_pandas_model(odmantic_engine, tmp_workdir):
    """Test updating a stored pandas model."""
    df1 = pd.DataFrame({"a": [1]})
    model = PandasModel.from_data_frame(df1)
    model.field_name = "update_test"
    await odmantic_engine.save(model)

    df2 = pd.DataFrame({"b": [2, 3]})
    model.table = df2
    await odmantic_engine.save(model)

    retrieved = await odmantic_engine.find_one(PandasModel, PandasModel.id == model.id)
    pd.testing.assert_frame_equal(df2, retrieved.table)
