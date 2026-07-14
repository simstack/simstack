.. _array_lists:

Array Lists
===========

SimStack II provides a specialized way to handle collections of arrays (typically numpy arrays) through the ``ArrayList`` and ``ArrayStorage`` models.

ArrayStorage
------------

``ArrayStorage`` is the model used to store an individual array. It handles compression, serialization, and flexible storage modes (in-memory or file-based).

**Key Features:**

* **Storage Modes**: Supports three storage modes via ``StorageModeEnum``:
    * ``AUTO`` (default): Automatically chooses between ``IN_MEMORY`` and ``FILE`` based on the array size.
    * ``IN_MEMORY``: Compresses and stores the array directly in the database (suitable for small arrays).
    * ``FILE``: Stores the array as a ``.npy`` file using ``FileStack`` (suitable for large arrays).
* **Automatic Fallback**: In ``AUTO`` mode, if the compressed array size exceeds the MongoDB document limit (approx. 16MB), it automatically falls back to ``FILE`` mode.
* **Shape Preservation**: Automatically stores and restores the shape of the array.
* **Property Access**: Use the ``array`` property to get or set the numpy array directly.

**Usage:**

.. code-block:: python

   import numpy as np
   from simstack.models.array_storage import ArrayStorage, StorageModeEnum

   # Default AUTO mode (will choose IN_MEMORY for this small array)
   storage = ArrayStorage(field_name="my_array")
   storage.array = np.random.rand(10, 10)

   # Explicit FILE mode
   large_storage = ArrayStorage(field_name="large_data", storage_mode=StorageModeEnum.FILE)
   large_storage.array = np.random.rand(1000, 1000)

ArrayList
---------

``ArrayList`` is a top-level ``Model`` that stores a collection of ``ArrayStorage`` objects. Like ``FileListModel``, it inherits from ``ObjectListMixin``, providing a familiar list-like interface.

**Usage:**

.. code-block:: python

   from simstack.models.array_list import ArrayList
   from simstack.models.array_storage import ArrayStorage
   import numpy as np

   array_list = ArrayList()
   
   storage = ArrayStorage(field_name="first")
   storage.array = np.zeros((5, 5))
   
   array_list.append(storage)

API Reference
-------------

.. autoclass:: simstack.models.array_list.ArrayList
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: simstack.models.array_storage.ArrayStorage
   :members:
   :undoc-members:
   :show-inheritance:
