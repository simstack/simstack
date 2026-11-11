.. _persisting_data:

Persisting and Visualizing Data
===============================

.. note::
    This section is intended for node-developers.

SimStack Server owns scientific records, task history and object storage.
Workflow code uses Pydantic domain models from ``simstack.models.document``
and explicit HTTP operations through ``context.server.values``. Runner and SDK
have no database engine, persistence facade or implicit database loading.

To customize the appearance of the models in the UI, the model definition
should be decorated by ``@simstack_model``
(:func:`simstack.core.simstack_model.simstack_model`)
which provides ui-related functionality discussed below
(:ref:`model-appearance`)

Model Definitions
~~~~~~~~~~~~~~~~~~

Domain models use Pydantic validation. ``Reference`` marks an explicit scientific
record reference; nested models without it are independent snapshots.

A basic model definition look like:

.. code-block:: python

   from typing import Optional, List
   from simstack.models.document import Model, EmbeddedModel, Field, Reference
   from simstack.core.simstack_model import simstack_model

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
        other_model: OtherModel = Reference()
        small_data: SmallData  # independent embedded value




.. important::
   - The ``@node`` decorator publishes scientific inputs and outputs through Server.
     Outside node execution, use ``await context.server.values.publish_value(value)``.
   - Resolve records explicitly with ``await context.server.values.get_value(ModelType, id)``.
     Constructing a model or reading a field performs no network I/O.

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

And the parallel execution utilities (see :doc:`mass_runner` and :doc:`fire_and_forget_runner`):

- MassRunner
- FireAndForgetRunner

:class:`simstack.models.array_storage.ArrayStorage` is an example of a class which
packs its content by serializing and deserializing it. This is the
recommended procedure for all content which MongoDB cannot natively store. To
interact with such classes, the developer has to provide access functions and
a member function ``custom_model_dump`` which the fast-api server to convert
the content of the class into a dict which can be visualized in the GUI.

.. _model-appearance:

Model Appearance in the GUI
---------------------------

The :class:`simstack.models.models.ModelTable` table in the database stores
for each class a json-schema (derived from the pydantic json-schema) and a
``ui_scheme`` which is created by
:func:`simstack.core.model_table.make_model_table`.

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
| from_dict     | create model instance from dict     | domain model     |
+---------------+-------------------------------------+------------------+
| from_model    | copy constructor                    | model copy       |
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
