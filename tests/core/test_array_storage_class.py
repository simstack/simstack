import numpy as np
import pytest
from simstack.models.array_storage import ArrayStorage


def test_set_array():
    """Test storing a numpy array in ArrayStorage"""
    array_model = ArrayStorage(name="test_array")
    array = np.array([[1, 2, 3], [4, 5, 6]])
    array_model.set_array(array)

    assert array_model.name == "test_array", "Name was not correctly set"
    assert array_model.shape == "2,3", "Shape was not correctly stored"
    assert (
        array_model.data_json == "[1, 2, 3, 4, 5, 6]"
    ), "Array data was not correctly stored as JSON"


def test_get_array():
    """Test retrieving a numpy array from ArrayStorage"""
    array_model = ArrayStorage(
        name="test_array", shape="2,3", data_json="[1, 2, 3, 4, 5, 6]"
    )
    array = array_model.get_array()

    expected = np.array([[1, 2, 3], [4, 5, 6]])
    np.testing.assert_array_equal(
        array, expected, "Retrieved array does not match the stored data"
    )


def test_name_update():
    """Test updating the name of the array in ArrayStorage"""
    array_model = ArrayStorage(
        name="initial_name", shape="2,2", data_json="[1, 2, 3, 4]"
    )
    array_model.name = "updated_name"

    assert (
        array_model.name == "updated_name"
    ), "ArrayStorage name did not update correctly"


def test_invalid_shape():
    """Test handling of invalid shape during array retrieval"""
    array_model = ArrayStorage(
        name="test_array", shape="2,invalid", data_json="[1, 2, 3, 4]"
    )
    with pytest.raises(ValueError, match="invalid literal for int"):
        array_model.get_array()
