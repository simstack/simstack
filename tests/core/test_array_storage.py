# test_array_storage.py
import numpy as np
import pytest
from simstack.models.array_storage import ArrayStorage


@pytest.mark.asyncio
async def test_create_array_storage(odmantic_engine):
    """Test creating and storing a new array."""
    # Create a test array
    test_array = np.array([[1, 2, 3], [4, 5, 6]])

    # Create storage object
    storage = ArrayStorage(name="test_matrix")
    storage.set_array(test_array)

    await odmantic_engine.save(storage)
    # Check it was stored with an ID
    assert storage.id is not None


@pytest.mark.asyncio
async def test_retrieve_array_storage(odmantic_engine):
    """Test retrieving a stored array."""
    # Create and store test array
    test_array = np.array([[1, 2, 3], [4, 5, 6]])
    storage = ArrayStorage(name="retrieve_test")
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
async def test_update_array_storage(odmantic_engine):
    """Test updating a stored array."""
    # Create and store initial array
    initial_array = np.array([1, 2, 3])
    storage = ArrayStorage(name="update_test")
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
async def test_query_by_name(odmantic_engine):
    """Test querying arrays by name."""
    # Create multiple array records
    arrays = [
        (np.array([1, 2, 3]), "array1"),
        (np.array([[4, 5], [6, 7]]), "array2"),
        (np.array([[[8, 9], [10, 11]]]), "array3"),
    ]

    # Store all arrays
    for arr, name in arrays:
        storage = ArrayStorage(name=name)
        storage.set_array(arr)
        await odmantic_engine.save(storage)

    # Query by name
    result = await odmantic_engine.find_one(ArrayStorage, ArrayStorage.name == "array2")

    # Check correct array was retrieved
    assert result.name == "array2"
    reconstructed = result.get_array()
    assert np.array_equal(reconstructed, arrays[1][0])


@pytest.mark.asyncio
async def test_store_complex_array(odmantic_engine):
    """Test storing and retrieving a complex array."""
    # Create a complex array with different data types
    complex_array = np.array([[1.5, 2.7, 3.1], [4.2, 5.5, 6.9]])

    # Store array
    storage = ArrayStorage(name="complex_array")
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
async def test_delete_array(odmantic_engine):
    """Test deleting an array from storage."""
    # Create and store array
    test_array = np.array([1, 2, 3])
    storage = ArrayStorage(name="delete_me")
    storage.set_array(test_array)
    await odmantic_engine.save(storage)

    # Get ID and delete
    storage_id = storage.id
    await odmantic_engine.delete(storage)

    # Check it's gone
    result = await odmantic_engine.find_one(ArrayStorage, ArrayStorage.id == storage_id)
    assert result is None
