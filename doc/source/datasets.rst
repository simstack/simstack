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
*   **Paged reading**: Stored sections load typed model pages through Server; ordinary section access materializes the section cache.
*   **Table representation**: It can automatically generate column definitions and table entries for UI components (like ag-grid).

To add data to a section, you can use the ``add_row`` method:

.. code-block:: python

    section.add_row({"model_a": instance_a, "model_b": instance_b})

Reading large sections in workflows
----------------------------------

Use the same typed iterator for ``DataSetSection`` and ``DataSetTupleSection``:

.. code-block:: python

    for page in dataset["results"].iter_model_pages(limit=100):
        for row in page:
            model = row.models["result"]  # a dict section
            process(model)

``iter_models(limit=100)`` yields individual rows from successive pages. Each row
has ``row_id``, ``position``, ``name`` and ``models``. For tuple sections,
``row.models`` is a tuple and ``row.name`` is ``None``. Follow the iterator rather
than collecting its results into a list. The page limit is between 1 and 100.

Unloaded stored sections use their pinned snapshot and a scoped Runner/provider;
iteration does not populate the full section cache. New sections and materialized
working buffers read already hydrated models in bounded pages without a remote
provider. Working-buffer pages have ``revision=None`` until publication. Do not
change section membership during iteration. Raw legacy references require
repository hydration before typed iteration.

The returned models are detached read views. To transform a dataset, create new
models with new IDs for changed values and add them to a new output section;
changing an iterator result does not update the input section. This also keeps
upstream source versions immutable. Building a new output section or a complete
plot can still consume memory proportional to the output size.

``section.items()``, ``values()``, normal iteration, indexing, ``get_item()``,
``get_all_model_groups()`` and ``section.data`` retain their full-section behavior.
Reading ``dataset.sections`` or ``dataset.items()`` only enumerates sections;
``len(section)`` reads the stored row count without loading its models.

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
