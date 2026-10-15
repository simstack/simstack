from typing import ClassVar

from simstack.models.document import EmbeddedModel, Model

from simstack.core.node import compute_arg_hash


class TopLevelCustomHashInput(Model):
    field_name: str = "TopLevelCustomHashInput"
    value: int
    hash_calls: ClassVar[int] = 0

    def complex_hash(self) -> str:
        type(self).hash_calls += 1
        return f"custom-top-level:{self.value}"


class NestedHashTrap(EmbeddedModel):
    value: int

    def complex_hash(self) -> str:
        raise AssertionError("nested custom hashes must not be called")


class FanoutHashInput(Model):
    field_name: str = "FanoutHashInput"
    index: int
    payloads: list[NestedHashTrap]


def _fanout_hash_input(index: int, payload_count: int = 20) -> FanoutHashInput:
    return FanoutHashInput(
        index=index,
        payloads=[
            NestedHashTrap(value=index * payload_count + payload_index)
            for payload_index in range(payload_count)
        ],
    )


def test_compute_arg_hash_preserves_top_level_custom_hash_contract():
    TopLevelCustomHashInput.hash_calls = 0

    hash_1 = compute_arg_hash([TopLevelCustomHashInput(value=1)])
    hash_1_again = compute_arg_hash([TopLevelCustomHashInput(value=1)])
    hash_2 = compute_arg_hash([TopLevelCustomHashInput(value=2)])

    assert TopLevelCustomHashInput.hash_calls == 3
    assert hash_1 == hash_1_again
    assert hash_1 != hash_2


def test_nested_values_do_not_invoke_custom_hash_hooks():
    first = compute_arg_hash([_fanout_hash_input(1)])
    first_again = compute_arg_hash([_fanout_hash_input(1)])
    second = compute_arg_hash([_fanout_hash_input(2)])

    assert first == first_again
    assert first != second
