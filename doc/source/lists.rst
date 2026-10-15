.. _lists:

List Mixins
===========

Simstack provides two main mixin classes to simplify the creation of models that behave like lists of other models: ``GenericListMixin`` and ``ObjectListMixin``. These mixins are located in ``simstack.models.base_lists``.

GenericListMixin
----------------

The ``GenericListMixin`` is intended for lists of items that are stored directly within the model, such as ``EmbeddedModel`` instances or basic Python types (strings, floats, etc.). It provides standard Python list-like behavior.

Requirements:
~~~~~~~~~~~~~

To use ``GenericListMixin``, your class must:

1. Inherit from ``GenericListMixin[T]``.
2. Have an attribute named ``elements`` of type ``List[T]``.

Example:
~~~~~~~~

.. code-block:: python

   from typing import List
   from simstack.models import simstack_model
   from simstack.models.document import Field, Model
   from simstack.models.base_lists import GenericListMixin

   @simstack_model
   class StringList(Model, GenericListMixin[str]):
       field_name: str = "string_list"
       elements: List[str] = Field(default_factory=list)

Available Methods:
~~~~~~~~~~~~~~~~~~

- ``append(element: T)``: Adds an element to the list.
- ``extend(elements: Union[List[T], GenericListMixin[T]])``: Extends the list with another list or another mixin-based list.
- ``insert(index: int, element: T)``: Inserts an element at a specific index.
- ``remove(element: T)``: Removes the first occurrence of an element.
- ``pop(index: int = -1)``: Removes and returns the element at the given index.
- ``clear()``: Removes all elements.
- Standard list methods: ``index``, ``count``, ``reverse``, ``sort``, ``copy``.
- Support for standard operators: ``len()``, ``[]`` (getitem/setitem/delitem), ``iter()``, ``in``, ``bool()``.

Search and Filter:
~~~~~~~~~~~~~~~~~~

The mixin also provides convenience methods for searching elements if they have a ``name`` or ``size`` attribute:

- ``find(pattern: str)``: Returns the first element where ``element.name == pattern``.
- ``find_all(pattern: str)``: Returns all elements where ``element.name`` matches the regex ``pattern``.
- ``filter_by_size(min_size, max_size)``: Returns elements with a ``size`` attribute within the specified range.
- ``filter_by_property(property_name, value)``: Returns elements where ``getattr(element, property_name) == value``.
- ``sort_by_name()``, ``sort_by_size()``: Sorts the list based on these attributes.


ObjectListMixin
---------------

The ``ObjectListMixin`` is designed for lists of separately persisted
``Model`` instances. Its document field stores their domain ``ObjectId``
values, while an in-memory cache provides normal model instances to node code.
The server prepares that dependency closure and an API-only runner hydrates the
cache from the task bundle; the runner does not load list elements from a
database.

Requirements:
~~~~~~~~~~~~~

To use ``ObjectListMixin``, your class must:

1. Inherit from ``ObjectListMixin[T]`` where ``T`` is a
   :class:`simstack.models.document.Model`.
2. Have an attribute named ``elements`` of type ``List[ObjectId]``.

Example:
~~~~~~~~

.. code-block:: python

   from typing import List
   from simstack.models import simstack_model, StringData
   from simstack.models.document import Field, Model, ObjectId
   from simstack.models.base_lists import ObjectListMixin

   @simstack_model
   class StringDataList(Model, ObjectListMixin[StringData]):
       field_name: str = "string_data_list"
       elements: List[ObjectId] = Field(default_factory=list)

Concrete Implementations
------------------------

Several core SimStack models utilize these mixins:

*   :doc:`files`: ``FileList`` and ``FileListModel`` use ``ObjectListMixin[FileStack]``.
*   :doc:`array_lists`: ``ArrayList`` uses ``ObjectListMixin[ArrayStorage]``.
*   :doc:`tables`: ``SimpleTable`` (while not using list mixins directly, it's a related data structure).

Available Methods:
~~~~~~~~~~~~~~~~~~

- ``append(element: T)``: Adds the model to the cache and its ID to
  ``elements``.
- ``extend(elements)``: Appends several already constructed models.
- ``get(index: int) -> T``: Returns a hydrated cached model; it fails if the
  requested entry was not supplied in the dependency bundle.
- ``find(pattern)`` and ``find_all(pattern)``: Search the hydrated cache by
  ``name``.
- ``len(my_list)``: Returns the number of stored IDs.
- ``my_list[index]``: Returns a hydrated model when available, otherwise its
  ``ObjectId``.
- ``element in my_list``: Checks a model, ``ObjectId``, or hydrated value.

List models are persistence-neutral values. The server repository hydrates
referenced elements before a task is dispatched and persists committed results
after execution. There are no model-level ``save`` or postprocess hooks, and an
API-only runner never receives a repository.
