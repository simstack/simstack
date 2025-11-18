.. _persisting_data:

Persisting and Visualizing Data
===============================

.. note::

This section is intended for node-developers.

Simstack persists data using a MongoDB database, which is exposed via the
fast-api server to a web-frontend. Each user has a dedicated database
on a MongoDB server which is configured in the configuration file
(:ref:`configuration-file`).

To facilitate access to the database in the python workflows, Simstack II uses the odmantic
package to provide an object-oriented access to the data via odmantic
Models. Odmantic Models are drived from pydantic (V2) classes and developers
of nodes (:ref:`workflows`) can define their own classes deriving from Model.

To customize the appearance of the models in the UI, the model definition
should be decorated by ``@simstack_model``
(:func:`simstack.core.simstack_model.simstack_model`)
which provides ui-related functionality discussed below
(:ref:`model-appearance`)

Model Definitions
~~~~~~~~~~~~~~~~~~

For detailed information on odmantic models, consult the odmantic
documentation.

A basic model definition look like:

.. code-block:: python

   from typing import Optional, List
   from odmantic import Model, EmbeddedModel, Field

   @simstack_model
   class OtherModel(Model):
        observations: List[float]
        max: float

   @simstack_model
   class SmallData(EmbeddedModel)
        number: int

   @simstack_model
   class ComplexData(Model)
        counter: int = Field(default=1)
        limit: Optional[int] = None
        other_model: OtherModel = Reference() # this defines a reference to the
                                              # other table in the db
                                              # loading ComplexData will load
                                              # the other model too
        small_data: SmallData # directly stored in the complex_data table



.. important::
   - The node developer does not have explicitly save models to the db, the
     @node decorator will take care of this.
   - There is **no Model inheritance**, which is a real pain.
   - ODMantic Issue #484: https://github.com/art049/odmantic/issues/484

.. note::
    Even though its formally allowed do not use Dict[str,Any] in models,
    because the UI behavior is unpredictable (see below).
    Dict[str,known_type] is ok.

**Standard Models**

:mod:`simstack.models.models` provides basic models:

- IntData
- StrData
- FloatData
- ArrayStorage
- ArrayList = List[ArrayStorage]

:class:`simstack.models.models.ArrayStorage` is an example of a class which
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
| from_dict     | create model instance from dict     | odmantic func    |
+---------------+-------------------------------------+------------------+
| from_model    | copy constructor                    | odmantic default |
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
