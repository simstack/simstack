File Management
===============

SimStack II provides a robust way to handle files within workflows. The core model for file management is ``FileStack``, which manages the storage and retrieval of individual files across different computational resources. For collections of files, ``FileList`` and ``FileListModel`` are provided.

FileStack
---------

A ``FileStack`` represents a single file that can be stored in the database (in-memory) or tracked on various computational resources.

**Key Features:**

* **Storage Flexibility:** Files can be stored directly in MongoDB (compressed) or kept on disk at various locations.
* **Automatic Transfer:** When a node requires a ``FileStack`` that is not locally available, SimStack automatically handles the transfer.
* **Hashing:** Supports content-based hashing for verification and caching.

**Usage:**

To create a ``FileStack`` from a local file:

.. code-block:: python

   from simstack.models.files import FileStack

   # Create a FileStack and store its content in the database
   file_stack = FileStack.from_local_file("path/to/my_file.txt", in_memory=True)

To create a ``FileStack`` from a string:

.. code-block:: python

   file_stack = FileStack.from_string("Hello SimStack!", "hello.txt")

To retrieve the file in a node:

.. code-block:: python

   @node
   def process_file(input_file: FileStack, **kwargs):
       # This ensures the file is available locally
       local_path = input_file.get()
       
       with open(local_path, "r") as f:
           content = f.read()
       ...

FileList and FileListModel
--------------------------

When dealing with multiple files, you can use ``FileList`` or ``FileListModel``.

FileList (Embedded)
~~~~~~~~~~~~~~~~~~~

``FileList`` is an ``EmbeddedModel`` that stores a list of ``FileStack`` objects directly within the parent model. This is suitable for smaller collections of files where you want to keep everything together.

.. code-block:: python

   from simstack.models.file_list import FileList
   from simstack.models.files import FileStack

   file_list = FileList()
   file_list.append(FileStack.from_local_file("file1.txt"))
   file_list.append(FileStack.from_local_file("file2.txt"))

FileListModel (Referenced)
~~~~~~~~~~~~~~~~~~~~~~~~~~

``FileListModel`` is a top-level ``Model`` that stores a list of ``ObjectId`` references to ``FileStack`` objects. This is preferred for large collections or when ``FileStack`` objects need to be shared across multiple models. It uses ``ObjectListMixin`` for asynchronous database access.

.. code-block:: python

   from simstack.models.file_list import FileListModel

   file_list_model = FileListModel()
   # Note: append is async for FileListModel
   await file_list_model.append(FileStack.from_local_file("large_file.dat"))

Methods and Mixins
~~~~~~~~~~~~~~~~~~

Both ``FileList`` and ``FileListModel`` provide a rich set of methods for managing the collection, such as ``append()``, ``extend()``, ``find()`` (by regex on filename), and ``filter_by_size()``.

For more details on the underlying list behaviors, see :doc:`lists`.

External and Internal Files and Resources
=========================================

Workflows need access to external resources, such as files, databases, and URLs.
External resources are only input and otherwise not managed. Internal resources, mostly
files, are managed by the workflow by certain specfications.

External resources differ from internal resources in that we cannot decide based
on the data describing the resource, whether its "value" or content has changed
since the last call.


Input Data
~~~~~~~~~~

Files can (mostly) be hashed. For all other use cases an intermediate storage (electron) can be implemented that
stores the "query" and the "result" of the query (assuming that the query and the result can be hashed).
This service electron returns the result with a flag whether the result has changed.


.. code-block:: python

    def complex_compute(data, reuse=True):
        # complex computation based on data
        return result

    def compute_electron(url):
        result, changed = get_external(url)
        if changed:
            result = complex_compute(url)
            electron.set(hash, result)
        return result

    def get_external(query):
        hash = hash(query)
        result, changed = get(hash)
        if changed:
            result = query_data(query)
            electron.set(hash, result)
        return result


## Output Data

Only Output Data is generated in SimStack II. Because "human readable" data is core concept of the new version all
digestible output should be parsed to JSON. The question is how to handle large-scale data, which is costly to
generate and which may have un-anticipated uses later on. An example would be MD trajectories.

This data should be handled as part of the research data management plan, which we plan to implement as an integral
part of the WF environment anyway. Every WANO will upload all inputs and outputs to the RDM storage anyway.
The JSON output should thus contain records of all files that have been uploaded.

The issue arising with large data is whether it makes sense to pass only the remote info
When a WANO is re-executed