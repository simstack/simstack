"""Actual model graphs hydrate from API data without database access or caps."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

import pytest

from simstack.core.runner_execution.errors import RunnerTaskPayloadError
from simstack.core.runner_execution.hydration import _hydrate_entries
from simstack.core.runner_execution.serialization import (
    _document_dependencies,
    _model_document,
)
from simstack.models.document import EmbeddedModel, Field, Model, ObjectId, Reference
from simstack.util.object_list_mixin import ObjectListMixin
from tests.without_context.test_runner_task_execution import _MappingTable


class Number(Model):
    value: int


class NumberList(Model, ObjectListMixin[Number]):
    elements: list[ObjectId] = Field(default_factory=list)


class Selection(EmbeddedModel):
    selected: Number = Reference()
    numbers: NumberList = Reference()


class OtherSelection(EmbeddedModel):
    kind: Literal["other"] = "other"
    selected: Number = Reference()


class TaggedSelection(Selection):
    kind: Literal["selection"] = "selection"


class Envelope(Model):
    entries: dict[str, Selection]
    positional: tuple[Selection, int]
    repeated: list[Selection]
    tagged: TaggedSelection | OtherSelection


class ChainEntry(Model):
    value: int
    child: ChainEntry | None = Reference(default=None)


def configure_models(monkeypatch):
    mappings = [SimpleNamespace(name=model.__name__, mapping=f"{__name__}.{model.__name__}") for model in (Number, NumberList, Envelope, ChainEntry)]
    context = SimpleNamespace(model_mappings=_MappingTable(mappings))
    monkeypatch.setattr("simstack.core.runner_execution.mappings.context", context)


def entry(value):
    return {
        "variable_name": "value", "variable_mapping": f"{__name__}.{type(value).__name__}",
        "reference": str(value.id), "value": _model_document(value),
        "dependencies": _document_dependencies(value),
    }


@pytest.mark.parametrize("malformed_tuple", [False, True])
def test_embedded_references_in_maps_tuples_lists_unions_and_object_lists(monkeypatch, malformed_tuple):
    configure_models(monkeypatch)
    number = Number(value=7)
    numbers = NumberList(elements=[number.id])
    numbers._set_cache([number])
    selected = Selection(selected=number, numbers=numbers)
    original = Envelope(entries={"chosen": selected}, positional=(selected, 9), repeated=[selected], tagged=TaggedSelection(selected=number, numbers=numbers))
    payload = entry(original)
    assert payload["value"]["entries"]["chosen"]["selected"] == str(number.id)
    assert len(payload["dependencies"]) == 2
    if malformed_tuple:
        payload["value"]["positional"].append("unexpected")
        with pytest.raises(RunnerTaskPayloadError):
            _hydrate_entries([payload], kind="inputs")
        return
    restored = _hydrate_entries([payload], kind="inputs")[0][1]
    assert restored.entries["chosen"].selected.value == 7
    assert restored.positional[0].numbers.get(0).value == 7
    assert restored.repeated[0].selected is restored.entries["chosen"].selected
    assert restored.tagged.kind == "selection"


def test_reference_chain_beyond_python_recursive_stack_hydrates_and_serializes(monkeypatch):
    configure_models(monkeypatch)
    original = ChainEntry(value=0)
    for index in range(1, 1500):
        original = ChainEntry(value=index, child=original)
    payload = entry(original)
    assert len(payload["dependencies"]) == 1499
    restored = _hydrate_entries([payload], kind="inputs")[0][1]
    count = 0
    while restored is not None:
        count += 1
        restored = restored.child
    assert count == 1500


def test_reference_cycles_are_rejected_without_recursing(monkeypatch):
    configure_models(monkeypatch)
    original = ChainEntry(value=1)
    payload = entry(original)
    payload["value"]["child"] = str(original.id)
    with pytest.raises(RunnerTaskPayloadError, match="Cyclic model references"):
        _hydrate_entries([payload], kind="inputs")
