from simstack.core.node import compute_arg_hash
import hashlib

from simstack.core.hash import complex_hash_function, hash_iterable
from simstack.domain.input_hashing import hashable_inputs
from simstack.models.document import Model


class HashInput(Model):
    value: int


class ExtraHashInput(Model):
    value: int
    model_config = {"extra": "allow"}


def test_input_hash_ignores_identity_and_runtime_bookkeeping():
    value = HashInput(value=1)
    original_hash = compute_arg_hash([value])
    value.__dict__["arbitrary_backend_bookkeeping"] = ("owner", str(value.id))
    assert compute_arg_hash([value]) == original_hash
    assert compute_arg_hash([HashInput(value=1)]) == original_hash
    assert compute_arg_hash([HashInput(value=2)]) != original_hash


def test_input_hash_includes_intentional_model_extras():
    value = ExtraHashInput(value=1, user_extra="a")
    assert hashable_inputs(value) == {"value": 1, "user_extra": "a"}
    assert compute_arg_hash([value]) != compute_arg_hash([ExtraHashInput(value=1, user_extra="b")])


def test_complex_model_hash_uses_declared_fields_and_extras_once():
    value = ExtraHashInput(value=1, user_extra="a")
    original_hash = complex_hash_function(value)
    value.__dict__["arbitrary_backend_bookkeeping"] = ("owner", str(value.id))
    assert complex_hash_function(value) == original_hash
    assert complex_hash_function(ExtraHashInput(value=1, user_extra="a")) == original_hash
    assert complex_hash_function(ExtraHashInput(value=1, user_extra="b")) != original_hash


def test_iterable_hash_includes_values_after_the_old_arbitrary_prefix_limit():
    first = [0] * 10005
    second = first.copy()
    second[-1] = 1
    assert complex_hash_function(first) != complex_hash_function(second)
    assert hash_iterable(first) != hash_iterable(second)


def test_streamed_hash_retains_the_existing_short_iterable_representation():
    values = [1, 2, 3]
    expected = hashlib.sha256("".join(f"{index}:{hash(value)}" for index, value in enumerate(values)).encode()).hexdigest()
    assert complex_hash_function(values) == expected
