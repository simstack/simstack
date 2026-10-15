"""Typed reference containers resolve their exact document leaves without DB IO."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from simstack.core.runner_execution.errors import RunnerTaskPayloadError
from simstack.core.runner_execution.hydration import _hydrate_entries
from simstack.core.runner_execution.references import transform_model_references
from simstack.models.document import EmbeddedModel, Model, Reference
from simstack.models.document.reference_values import (
    async_transform_reference_values,
    transform_reference_values,
)
from tests.without_context.test_runner_task_execution import _MappingTable


class First(Model):
    value: int


class Second(Model):
    label: str


class Selection(EmbeddedModel):
    selected: First = Reference()
    ordinary: dict[str, str]


class Envelope(Model):
    named: dict[str, list[First | None]] = Reference()
    positions: tuple[First, Second] = Reference()
    varying: tuple[First, ...] = Reference()
    maybe: First | None = Reference(default=None)
    embedded: Selection
    ordinary: dict[str, str]


class RecursiveContainer(Model):
    children: list[RecursiveContainer] = Reference(default_factory=list)


def configure_models(monkeypatch):
    entries = [
        SimpleNamespace(name=cls.__name__, mapping=f"{__name__}.{cls.__name__}")
        for cls in (First, Second, Envelope, RecursiveContainer)
    ]
    monkeypatch.setattr(
        "simstack.core.runner_execution.mappings.context",
        SimpleNamespace(model_mappings=_MappingTable(entries)),
    )


def payload(first, second):
    identifier = "a" * 24
    return {
        "variable_name": "result", "variable_mapping": f"{__name__}.Envelope",
        "reference": identifier,
        "value": {
            "id": identifier,
            "named": {"chosen": [str(first.id), None, str(first.id)]},
            "positions": [str(first.id), str(second.id)],
            "varying": [str(first.id), str(first.id)],
            "maybe": None,
            "embedded": {"selected": str(first.id), "ordinary": {"untouched": str(second.id)}},
            "ordinary": {"untouched": str(second.id)},
        },
        "dependencies": [
            {"variable_mapping": f"{__name__}.{type(model).__name__}",
             "reference": str(model.id), "value": {"id": str(model.id), **fields}}
            for model, fields in ((first, {"value": first.value}), (second, {"label": second.label}))
        ],
    }


def test_runtime_hydrates_exact_leaves_and_preserves_ordinary_embedded_values(monkeypatch):
    configure_models(monkeypatch)
    first, second = First(value=7), Second(label="second")
    restored = _hydrate_entries([payload(first, second)], kind="results")[0][1]
    assert restored.named["chosen"][0].value == 7
    assert restored.named["chosen"][1] is None
    assert restored.named["chosen"][0] is restored.named["chosen"][2]
    assert restored.positions[0] is restored.named["chosen"][0]
    assert isinstance(restored.positions[1], Second)
    assert restored.positions[1].label == "second"
    assert isinstance(restored.varying, tuple)
    assert restored.varying[0] is restored.named["chosen"][0]
    assert restored.embedded.selected is restored.named["chosen"][0]
    assert restored.embedded.ordinary == {"untouched": str(second.id)}
    assert restored.ordinary == {"untouched": str(second.id)}


@pytest.mark.parametrize("problem", ["missing", "wrong_type", "wrong_parent_type", "bad_tuple"])
def test_runtime_container_graph_errors_are_not_hidden(monkeypatch, problem):
    configure_models(monkeypatch)
    first, second = First(value=7), Second(label="second")
    entry = payload(first, second)
    expected = None
    if problem == "missing":
        entry["value"]["named"]["chosen"][0] = "b" * 24
        expected = "missing referenced model"
    elif problem == "wrong_type":
        entry["value"]["positions"][0] = str(second.id)
        expected = "type mismatch"
    elif problem == "wrong_parent_type":
        # A parent reference cannot masquerade as a child of a different type.
        entry["value"]["named"]["chosen"][0] = entry["value"]["id"]
        expected = "type mismatch"
    else:
        entry["value"]["positions"].append(str(first.id))
        expected = "invalid length"
    with pytest.raises(RunnerTaskPayloadError, match=expected):
        _hydrate_entries([entry], kind="results")


def test_runtime_rejects_a_cycle_inside_a_reference_container(monkeypatch):
    configure_models(monkeypatch)
    identifier = "a" * 24
    entry = {
        "variable_name": "result", "variable_mapping": f"{__name__}.RecursiveContainer",
        "reference": identifier, "value": {"id": identifier, "children": [identifier]},
        "dependencies": [],
    }
    with pytest.raises(RunnerTaskPayloadError, match="Cyclic model references"):
        _hydrate_entries([entry], kind="results")


def test_shared_reference_helper_preserves_fixed_tuple_types_and_leaf_paths():
    first, second = First(value=1), Second(label="two")
    models = {str(first.id): first, str(second.id): second}
    calls = []

    def resolve(value, cls, path):
        calls.append((value, cls, path))
        return models[value]

    value = (str(first.id), {"right": [str(second.id), None]}, 9)
    result = transform_reference_values(
        value, tuple[First, dict[str, list[Second | None]], int], resolve, path="output",
    )
    assert result == (first, {"right": [second, None]}, 9)
    assert calls == [(str(first.id), First, "output[0]"),
                     (str(second.id), Second, "output[1]['right'][0]")]


@pytest.mark.asyncio
async def test_async_reference_helper_has_the_same_typed_leaf_contract():
    first = First(value=1)
    calls = []

    async def resolve(value, cls, path):
        calls.append((value, cls, path))
        return first

    restored = await async_transform_reference_values(
        {"nested": [str(first.id), None]}, dict[str, list[First | None]], resolve, path="output",
    )
    assert restored == {"nested": [first, None]}
    assert calls == [(str(first.id), First, "output['nested'][0]")]


@pytest.mark.parametrize("annotation,value", [
    (list[First], "not-a-list"),
    (dict[str, First], ["not-a-map"]),
    (tuple[First, Second], ["one-item"]),
    (First | Second, "ambiguous-id"),
    (list[First | Second], []),
    (list[int], [1]),
])
def test_invalid_reference_contracts_do_not_guess_or_drop_values(annotation, value):
    with pytest.raises(ValueError):
        transform_reference_values(value, annotation, lambda *args: pytest.fail("Unexpected resolution"), path="output")


def test_runtime_translation_preserves_payload_error_type_for_invalid_container():
    with pytest.raises(RunnerTaskPayloadError, match="must be a mapping"):
        transform_model_references({"named": "wrong"}, Envelope, lambda *args: None, path="Envelope")
