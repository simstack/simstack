import pytest
import numpy as np
from simstack.core.context import context
from simstack.models.array_list import ArrayList
from simstack.models.array_storage import ArrayStorage


@pytest.mark.asyncio
async def test_array_list_basic_operations(initialized_context):
    db = context.db
    engine = db.core_engine

    a1 = ArrayStorage(name="array1")
    a1.set_array(np.array([1, 2, 3]))
    await engine.save(a1)

    arr_list = ArrayList()
    arr_list.append(a1)

    # Check elements field
    assert len(arr_list.elements) == 1
    assert arr_list.elements[0] == a1.id

    # Test iteration
    items = [a for a in arr_list]
    assert len(items) == 1
    assert items[0].name == "array1"

    # Test persistence
    await engine.save(arr_list)
    fetched = await engine.find_one(ArrayList, ArrayList.id == arr_list.id)
    assert fetched is not None

    # Load cache
    await fetched.db_find_postprocess(db)
    assert len([a for a in fetched]) == 1


@pytest.mark.asyncio
async def test_array_list_initialization_with_data(initialized_context):
    engine = context.db.core_engine
    a1 = ArrayStorage(name="array1")
    a1.set_array(np.array([1, 2]))
    await engine.save(a1)

    # Initialize ArrayList with existing elements
    arr_list = ArrayList(elements=[a1])

    assert len(arr_list.elements) == 1
    assert arr_list.elements[0] == a1.id


@pytest.mark.asyncio
async def test_array_list_manipulation(initialized_context):
    engine = context.db.core_engine
    a1 = ArrayStorage(name="a1")
    a2 = ArrayStorage(name="a2")
    await engine.save(a1)
    await engine.save(a2)

    arr_list = ArrayList()

    # Use Mixin methods directly
    arr_list.append(a1)
    arr_list.append(a2)

    assert len(arr_list.elements) == 2

    # Test remove
    arr_list.remove(a1)

    assert len(arr_list.elements) == 1

    # Test clear
    arr_list.clear()
    assert len(arr_list.elements) == 0
