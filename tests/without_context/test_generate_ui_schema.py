from simstack.models import simstack_model
from simstack.models.document import EmbeddedModel, Model


def test_generate_ui_schema_uses_nested_model_import_path():
    @simstack_model
    class NestedPluginModel(EmbeddedModel):
        value: str = "default"

    @simstack_model
    class ParentModel(Model):
        nested: NestedPluginModel

    ui_schema = ParentModel.ui_schema()

    assert ui_schema["nested"] == {
        "ui:field": "GenericFormField",
        "ui:options": {
            "model": f"{NestedPluginModel.__module__}.NestedPluginModel",
            "accordion": "true",
        },
    }
