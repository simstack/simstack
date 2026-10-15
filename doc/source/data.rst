.. _persisting_data:

Persisting and Visualizing Data
===============================

.. note::
    This section is intended for node-developers.

Simstack domain data is represented by Pydantic models from
:mod:`simstack.models.document`. These classes contain validation,
serialization, reference metadata, and JSON-safe identifiers, but no database
client or active-record API. The FastAPI server owns the persistence repository
and any MongoDB-specific conversion. Remote runners exchange model documents
with that server through the runner API and must not connect to the database.

Node developers (:ref:`workflows`) can define document models by deriving from
:class:`simstack.models.document.Model`, or inline value objects by deriving
from :class:`simstack.models.document.EmbeddedModel`.

To customize the appearance of the models in the UI, the model definition
should be decorated by ``@simstack_model``
(:func:`simstack.models.simstack_model.simstack_model`)
which provides ui-related functionality discussed below
(:ref:`model-appearance`)

Model Definitions
~~~~~~~~~~~~~~~~~~

A basic model definition looks like:

.. code-block:: python

   from typing import Optional, List
   from simstack.models import simstack_model
   from simstack.models.document import EmbeddedModel, Field, Model, Reference

   @simstack_model
   class OtherModel(Model):
        observations: List[float]
        max: float

   @simstack_model
   class SmallData(EmbeddedModel):
        number: int

   @simstack_model
   class ComplexData(Model):
        counter: int = Field(default=1)
        limit: Optional[int] = None
        # A separately persisted document reference. The server resolves it.
        other_model: OtherModel = Reference()
        # Embedded values are stored inline in the ComplexData document.
        small_data: SmallData



.. important::
   - Node code does not save models directly. The node execution contract sends
     declared inputs and results through the server API.
   - ``Reference()`` is for independently persisted ``Model`` documents.
     Use ``EmbeddedModel`` for values owned by their parent document.
   - Model inheritance follows normal Pydantic behavior. Give persisted model
     families an intentional collection contract when they share storage.

.. note::
    Even though its formally allowed do not use Dict[str,Any] in models,
    because the UI behavior is unpredictable (see below).
    Dict[str,known_type] is ok.

**Standard Models**

:mod:`simstack.models.base_types` provides basic models:

- IntData
- StrData
- FloatData
- BooleanData

And :mod:`simstack.models.array_storage`:

- ArrayStorage
- ArrayList

And the list mixins for creating custom list models (see :doc:`lists`):

- GenericListMixin
- ObjectListMixin

And for file management (see :doc:`files`):

- FileStack
- FileList
- FileListModel

:class:`simstack.models.array_storage.ArrayStorage` is an example of a class
which packs its content into ordinary document fields. Values that need a wire
representation should use the public document codec rather than importing BSON
or a database driver. ``model_dump_doc`` produces the storage-shaped,
JSON-safe document consumed by the server boundary.

.. _model-appearance:

Model Appearance in the GUI
---------------------------

The server's :class:`simstack.models.models.ModelMapping` registry stores a
JSON Schema (derived from Pydantic) and a UI schema for each registered class.
The records are created by
:func:`simstack.tables.model_table.make_model_table`.

The UI used a package react-json-forms (rjsf) to interpret these schema to
provide input and output representations of the model. When queried the
routes of the fast-api server provide (get) or store (post) dicts that are
compatible with the json-schema. 8

**Class Methods**

+---------------+-------------------------------------+------------------+
| Function Name | Purpose                             | default          |
+---------------+-------------------------------------+------------------+
| json_schema   | json schema for rjsf                | pydantic-schema  |
+---------------+-------------------------------------+------------------+
| ui_schema     | ui_schema dict for rjsf             | hide id          |
+---------------+-------------------------------------+------------------+
| ui_make_title | title for the model in ui           | class name       |
+---------------+-------------------------------------+------------------+
| from_dict     | create model instance from dict     | Pydantic helper  |
+---------------+-------------------------------------+------------------+
| from_model    | copy constructor                    | Pydantic helper  |
+---------------+-------------------------------------+------------------+

**Instance Functions**

+-------------------+--------------------------------+------------------+
| Function Name     | Purpose                        | default          |
+-------------------+--------------------------------+------------------+
| custom_model_dump | convert model to dict          | model_dump       |
+-------------------+--------------------------------+------------------+

Advanced Tools
~~~~~~~~~~~~~~

Conditional Schema

.. code-block:: python

   schema['properties']['test'] = {
         "type": "object",
         "oneOf": [
             {
                 "properties": {
                     "lorem": {
                         "type": "string"
                     }
                 },
                 "required": [
                     "lorem"
                 ]
             },
             {
                 "properties": {
                     "ipsum": {
                         "type": "string"
                     }
                 },
                 "required": [
                     "ipsum"
                 ]
             }
