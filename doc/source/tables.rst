.. _tables:

Tables
======

SimStack II provides a ``SimpleTable`` model to handle and display tabular data, especially for UI representation in ag-grid.

SimpleTable
-----------

The ``SimpleTable`` model allows you to define a table with headings, data types for columns, and rows of data.

**Key Features:**

* **UI Ready**: Designed to be used with a specialized UI field (``SimpleTableField``) that renders the data in an ag-grid component.
* **Flexible Rows**: Rows are stored as dictionaries, making it easy to map data to column headers.
* **Type Information**: Allows specifying the type of each column for better UI rendering or validation.

**Usage:**

.. code-block:: python

   from simstack.models.simple_table import SimpleTable

   table = SimpleTable(name="Experimental Results")
   
   # Add columns with their types
   table.add_column("Material", "string")
   table.add_column("Temperature", "number")
   table.add_column("Pressure", "number")
   
   # Add rows as dictionaries
   table.add_row({
       "Material": "Silicon",
       "Temperature": 300,
       "Pressure": 1.0
   })
   table.add_row({
       "Material": "Germanium",
       "Temperature": 350,
       "Pressure": 1.2
   })

API Reference
-------------

.. autoclass:: simstack.models.simple_table.SimpleTable
   :members:
   :undoc-members:
   :show-inheritance:
