.. _datasets:

Datasets
========

SimStack II uses a structured way to handle datasets through the ``DataSet``, ``DataSetSection``, and ``DataSetSelection`` models defined in ``simstack.models.dataset``.

DataSet
-------

The ``DataSet`` model is the top-level container for data.


It consists of:

*   **metadata**: A ``DataSetMetadata`` object that defines the type and validation rules for the dataset.
*   **sections**: A dictionary of ``DataSetSection`` objects, keyed by section name.

It behaves like a dictionary of sections. Think of a dataset as an excel file, where each ``DataSetSection`` is a sheet.

Metadata
--------

Metadata in SimStack II is handled by the ``DataSetMetadata`` and ``DataSetMetadataTemplate`` classes.

- **DataSetMetadata**: This is an ``EmbeddedModel`` used within a ``DataSet``. It behaves like a dictionary and stores key-value pairs of information about the dataset (e.g., experimental conditions, parameters). It also ensures structural consistency of the data.
- **DataSetMetadataTemplate**: This model defines the expected structure and schema for a specific type of dataset metadata. When a new ``DataSet`` is saved, its metadata is validated against the corresponding template.

The metadata provides:
*   **Validation**: Ensures that all datasets of the same type have consistent metadata keys and value types.
*   **JSON Schema**: Automatically generates JSON schemas for the stored metadata.
*   **Dict-like API**: Supports standard dictionary operations like ``__getitem__``, ``keys()``, ``items()``, and ``update()``.

DataSetSection
--------------

A ``DataSetSection`` represents a collection of rows within a dataset. Each row is a dictionary of models, but only the model ids are stored when saving.


Key features:
*   **Type consistency**: All rows in a section must have the same model types for the same keys.
*   **Lazy loading**: Models are cached and only loaded from the database when needed.
*   **Table representation**: It can automatically generate column definitions and table entries for UI components (like ag-grid).

To add data to a section, you can use the ``add_row`` method:

.. code-block:: python

    section.add_row({"model_a": instance_a, "model_b": instance_b})

DataSetSelection
----------------

The ``DataSetSelection`` model is used to reference specific items within a ``DataSet``. Instead of duplicating data, it stores the ``dataset_id`` and a list of indices for each section.

This is particularly useful for workflows where a user selects a subset of a dataset to be processed by a subsequent node.

Example usage:

.. code-block:: python

    selection = DataSetSelection(dataset_id=my_dataset.id)
    selection.dataset_selection_fields.append(
        DataSetSelectionField(section_name="default", indices=[0, 2, 5])
    )

API Reference
-------------

.. autoclass:: simstack.models.dataset.DataSet
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: simstack.models.dataset.DataSetSection
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: simstack.models.dataset.DataSetSelection
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: simstack.models.dataset_metadata.DataSetMetadata
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: simstack.models.dataset_metadata.DataSetMetadataTemplate
   :members:
   :undoc-members:
   :show-inheritance:
