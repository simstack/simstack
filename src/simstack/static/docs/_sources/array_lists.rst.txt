.. _array_lists:

Array Lists
===========

SimStack II provides a specialized way to handle collections of arrays (typically numpy arrays) through the ``ArrayList`` and ``ArrayStorage`` models.

ArrayStorage
------------

``ArrayStorage`` is the model used to store an individual array. It handles compression and serialization of the array data.

**Key Features:**

* **Compression**: Array data is automatically compressed before being stored in the database.
* **Shape Preservation**: Automatically stores and restores the shape of the array.
* **Property Access**: Use the ``array`` property to get or set the numpy array directly.

**Usage:**

.. code-block:: python

   import numpy as np
   from simstack.models.array_storage import ArrayStorage

   storage = ArrayStorage(name="my_array")
   storage.array = np.random.rand(10, 10)

ArrayList
---------

``ArrayList`` is a top-level ``Model`` that stores a collection of ``ArrayStorage`` objects. Like ``FileListModel``, it inherits from ``ObjectListMixin``, providing a familiar list-like interface.

**Usage:**

.. code-block:: python

   from simstack.models.array_list import ArrayList
   from simstack.models.array_storage import ArrayStorage
   import numpy as np

   array_list = ArrayList()
   
   storage = ArrayStorage(name="first")
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
