# test_array_storage.py
import numpy as np
import pytest
import os
from pathlib import Path
from simstack.models.array_storage import ArrayStorage, StorageModeEnum
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
async def test_create_array_storage(odmantic_engine, tmp_workdir):
    """Test creating and storing a new array."""
    # Create a test array
    test_array = np.array([[1, 2, 3], [4, 5, 6]])

    # Create storage object
    storage = ArrayStorage(field_name="test_matrix")
    storage.set_array(test_array)

    await odmantic_engine.save(storage)
    # Check it was stored with an ID
    assert storage.id is not None
    assert storage.shape == "2,3"
    assert storage.storage_mode == StorageModeEnum.IN_MEMORY


@pytest.mark.asyncio
async def test_retrieve_array_storage(odmantic_engine, tmp_workdir):
    """Test retrieving a stored array."""
    # Create and store test array
    test_array = np.array([[1, 2, 3], [4, 5, 6]])
    storage = ArrayStorage(field_name="retrieve_test")
    storage.set_array(test_array)
    await odmantic_engine.save(storage)

    # Retrieve from database
    retrieved = await odmantic_engine.find_one(
        ArrayStorage, ArrayStorage.id == storage.id
    )

    # Get array back
    reconstructed = retrieved.get_array()

    # Check array is correctly reconstructed
    assert np.array_equal(test_array, reconstructed)
    assert reconstructed.shape == (2, 3)


@pytest.mark.asyncio
async def test_update_array_storage(odmantic_engine, tmp_workdir):
    """Test updating a stored array."""
    # Create and store initial array
    initial_array = np.array([1, 2, 3])
    storage = ArrayStorage(field_name="update_test")
    storage.set_array(initial_array)
    await odmantic_engine.save(storage)

    # Update with new array
    new_array = np.array([[4, 5], [6, 7]])
    storage.set_array(new_array)
    await odmantic_engine.save(storage)

    # Retrieve and check
    retrieved = await odmantic_engine.find_one(
        ArrayStorage, ArrayStorage.id == storage.id
    )

    reconstructed = retrieved.get_array()

    assert np.array_equal(new_array, reconstructed)
    assert reconstructed.shape == (2, 2)


@pytest.mark.asyncio
async def test_query_by_name(odmantic_engine, tmp_workdir):
    """Test querying arrays by name."""
    # Create multiple array records
    arrays = [
        (np.array([1, 2, 3]), "array1"),
        (np.array([[4, 5], [6, 7]]), "array2"),
        (np.array([[[8, 9], [10, 11]]]), "array3"),
    ]

    # Store all arrays
    for arr, name in arrays:
        storage = ArrayStorage(field_name=name)
        storage.set_array(arr)
        await odmantic_engine.save(storage)

    # Query by name
    result = await odmantic_engine.find_one(ArrayStorage, ArrayStorage.field_name == "array2")

    # Check correct array was retrieved
    assert result.field_name == "array2"
    reconstructed = result.get_array()
    assert np.array_equal(reconstructed, arrays[1][0])


@pytest.mark.asyncio
async def test_store_complex_array(odmantic_engine, tmp_workdir):
    """Test storing and retrieving a complex array."""
    # Create a complex array with different data types
    complex_array = np.array([[1.5, 2.7, 3.1], [4.2, 5.5, 6.9]])

    # Store array
    storage = ArrayStorage(field_name="complex_array")
    storage.set_array(complex_array)
    await odmantic_engine.save(storage)

    # Retrieve array
    retrieved = await odmantic_engine.find_one(
        ArrayStorage, ArrayStorage.id == storage.id
    )
    reconstructed = retrieved.get_array()

    # Verify array contents
    assert np.allclose(complex_array, reconstructed)
    assert reconstructed.dtype == complex_array.dtype


@pytest.mark.asyncio
async def test_delete_array(odmantic_engine, tmp_workdir):
    """Test deleting an array from storage."""
    # Create and store array
    test_array = np.array([1, 2, 3])
    storage = ArrayStorage(field_name="delete_me")
    storage.set_array(test_array)
    await odmantic_engine.save(storage)

    # Get ID and delete
    storage_id = storage.id
    await odmantic_engine.delete(storage)

    # Check it's gone
    result = await odmantic_engine.find_one(ArrayStorage, ArrayStorage.id == storage_id)
    assert result is None


def test_name_update():
    """Test updating the name of the array in ArrayStorage"""
    array_model = ArrayStorage(
        field_name="initial_name", shape="2,2", data_json="[1, 2, 3, 4]"
    )
    array_model.field_name = "updated_name"

    assert (
        array_model.field_name == "updated_name"
    ), "ArrayStorage field_name did not update correctly"


def test_invalid_shape():
    """Test handling of invalid shape during array retrieval"""
    # Note: we need to set storage_mode to something other than AUTO to avoid ValueError in get_array
    array_model = ArrayStorage(
        field_name="test_array", shape="2,invalid", data_json="[1, 2, 3, 4]",
        storage_mode=StorageModeEnum.IN_MEMORY
    )
    with pytest.raises(ValueError, match="invalid literal for int"):
        array_model.get_array()


@pytest.mark.asyncio
async def test_file_mode_storage(odmantic_engine, tmp_workdir):
    """Test forcing and using FILE storage mode."""
    test_array = np.array([[10, 20], [30, 40]])
    storage = ArrayStorage(field_name="file_mode_test", storage_mode=StorageModeEnum.FILE)
    
    storage.set_array(test_array)

    assert storage.storage_mode == StorageModeEnum.FILE
    assert storage.data_json is None
    assert storage.file_stack is not None

    await odmantic_engine.save(storage)

    retrieved = await odmantic_engine.find_one(ArrayStorage, ArrayStorage.id == storage.id)
    assert retrieved.storage_mode == StorageModeEnum.FILE
    assert retrieved.data_json is None

    reconstructed = retrieved.get_array()
    assert np.array_equal(test_array, reconstructed)


@pytest.mark.asyncio
async def test_large_array_auto_fallback_to_file(odmantic_engine, tmp_workdir):
    """Test that large arrays automatically fall back to FILE mode."""
    # Use larger array to trigger FILE mode fallback (>16MB).
    # float64 takes 8 bytes. 3*10^6 elements = 24MB.
    large_array = np.random.rand(1000, 3000) 
    
    storage = ArrayStorage(field_name="large_array", storage_mode=StorageModeEnum.AUTO)
    
    storage.set_array(large_array)

    assert storage.storage_mode == StorageModeEnum.FILE
    assert storage.file_stack is not None
    
    await odmantic_engine.save(storage)
    
    retrieved = await odmantic_engine.find_one(ArrayStorage, ArrayStorage.id == storage.id)
    assert retrieved.storage_mode == StorageModeEnum.FILE
    
    reconstructed = retrieved.get_array()
    assert np.array_equal(large_array, reconstructed)
