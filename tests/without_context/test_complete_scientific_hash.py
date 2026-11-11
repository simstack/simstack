"""Scientific cache keys must cover the full value, type, and structure."""

import os
import subprocess
import sys

import pytest

from simstack.core.hash import complex_hash_function, hash_iterable
from simstack.models.document import EmbeddedModel, Model


class ScientificPoint(EmbeddedModel):
    value: int


class ScientificSequence(Model):
    points: list[ScientificPoint]


@pytest.mark.parametrize("left,right", [
    (-1, -2), (-1.0, -2.0), (True, 1), (1, 1.0), (0.0, -0.0),
    ({1: 7}, {"1": 7}), ({}, []), ([], ()), ([1], (1,)),
    ({"value": b"1"}, {"value": "1"}),
])
def test_different_scientific_values_do_not_share_a_cache_key(left, right):
    assert complex_hash_function(left) != complex_hash_function(right)


@pytest.mark.parametrize("hash_function", [complex_hash_function, hash_iterable])
def test_complete_iterable_including_tail_and_length(hash_function):
    original = [0] * 10004
    changed = original.copy()
    changed[-1] = 1
    assert hash_function(original) == hash_function(original.copy())
    assert hash_function(original) != hash_function(changed)
    assert hash_function(original) != hash_function(original + [0])


def test_repeated_models_are_values_not_recursive_sentinels():
    first, second = ScientificPoint(value=1), ScientificPoint(value=2)
    original = ScientificSequence(points=[first, second, first])
    changed = ScientificSequence(points=[first, second, second])
    independent_copy = ScientificSequence(points=[
        ScientificPoint(value=1), ScientificPoint(value=2), ScientificPoint(value=1),
    ])
    assert complex_hash_function(original) == complex_hash_function(independent_copy)
    assert complex_hash_function(original) != complex_hash_function(changed)


def test_mapping_order_is_irrelevant_and_mixed_keys_are_supported():
    assert complex_hash_function({1: "one", "two": 2}) == complex_hash_function(
        {"two": 2, 1: "one"}
    )


def test_default_values_and_unordered_inputs_are_process_stable():
    script = (
        "from simstack.core.hash import complex_hash_function as h\n"
        "print(h({'optional': None, 'charge': -2, 'labels': {'alpha', 'beta', 'gamma'}}))\n"
    )
    hashes = [subprocess.check_output(
        [sys.executable, "-c", script], text=True,
        env={**os.environ, "PYTHONHASHSEED": str(seed)},
    ).strip() for seed in (1, 2, 3)]
    assert len(set(hashes)) == 1


@pytest.mark.parametrize("container", [[], {}])
def test_cyclic_inputs_fail_explicitly_instead_of_sharing_a_partial_key(container):
    if isinstance(container, list):
        container.append(container)
    else:
        container["self"] = container
    with pytest.raises(ValueError, match="cyclic"):
        complex_hash_function(container)
