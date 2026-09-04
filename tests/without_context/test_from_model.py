from odmantic import Model

from simstack.models import simstack_model


@simstack_model
class CopyProbe(Model):
    field_name: str = "CopyProbe"
    value: int = 1
    label: str = "a"


def test_from_model_copies_fields_and_assigns_new_id():
    source = CopyProbe(value=42, label="keep")
    copied = CopyProbe.from_model(source)

    assert copied is not source
    assert copied.id != source.id
    assert copied.value == 42
    assert copied.label == "keep"


def test_from_model_applies_field_overrides():
    source = CopyProbe(value=42, label="keep")
    copied = CopyProbe.from_model(source, value=7)

    assert copied.value == 7
    assert copied.label == "keep"
    assert copied.id != source.id
