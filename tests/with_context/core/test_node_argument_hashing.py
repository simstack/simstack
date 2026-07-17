import asyncio
import uuid
from typing import ClassVar

import pytest
from odmantic import EmbeddedModel, Model

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import compute_arg_hash, node
from simstack.models import FloatData, IntData
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import SlurmParameters


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


@node(
    resource="test",
    queue="slurm-queue",
    force_rerun=True,
    slurm_parameters=SlurmParameters(nodes=1, tasks_per_node=1),
)
async def hashing_fanout_child_in_tests(args: FanoutHashInput, **kwargs) -> FloatData:
    return FloatData(value=float(args.index))


@node(force_rerun=True)
async def hashing_fanout_parent_in_tests(args: IntData, **kwargs) -> FloatData:
    tasks = [
        hashing_fanout_child_in_tests(
            _fanout_hash_input(index),
            **kwargs,
        )
        for index in range(args.value)
    ]
    results = await asyncio.gather(*tasks)
    return FloatData(value=float(len(results)))


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


from simstack.models.models import NodeModel, ModelMapping
from simstack.models.parameters import Parameters

@pytest.mark.asyncio
async def test_async_parent_fanout_creates_slurm_children_with_nested_hash_traps(
    monkeypatch,initialized_context
):
    # Ensure model mappings for argument hashing
    for model_cls in [IntData, FanoutHashInput, FloatData]:
        mm = ModelMapping(
            name=model_cls.__name__,
            mapping=f"simstack.models:{model_cls.__name__}",
            collection_name=model_cls.__name__.lower()
        )
        # Check if already exists to avoid unique constraint error if conftest did it
        existing = await context.db.find_one(ModelMapping, ModelMapping.name == mm.name)
        if not existing:
            await context.db.save(mm)

    # Ensure node models are in the database
    for node_func in [hashing_fanout_parent_in_tests, hashing_fanout_child_in_tests]:
        nm = NodeModel(
            name=node_func.__name__,
            function_mapping=f"simstack.tests.with_context.core.test_node_argument_hashing:{node_func.__name__}",
            input_mappings=[],
            result_mappings=[],
            default_parameters=Parameters()
        )
        existing_nm = await context.db.find_one(NodeModel, NodeModel.name == nm.name)
        if not existing_nm:
            await context.db.save(nm)
        else:
            # Update mapping just in case
            existing_nm.function_mapping = nm.function_mapping
            await context.db.save(existing_nm)
    custom_name = f"hash-fanout-{uuid.uuid4()}"
    submitted_ids = []

    async def fake_submit_node(entry: NodeRegistry) -> None:
        submitted_ids.append(entry.id)
        entry.status = TaskStatus.COMPLETED
        entry.job_id = f"fake-slurm-{len(submitted_ids)}"
        await context.db.save(entry)

    monkeypatch.setattr("simstack.core.submit_node.submit_node", fake_submit_node)

    result = await hashing_fanout_parent_in_tests(
        IntData(value=50),
        custom_name=custom_name,
    )

    assert result.value == 50
    assert len(submitted_ids) == 50

    entries = await context.db.find(NodeRegistry)
    parent_entries = [
        entry
        for entry in entries
        if entry.name == "hashing_fanout_parent_in_tests"
        and entry.custom_name == custom_name
    ]
    assert len(parent_entries) == 1
    parent_entry = parent_entries[0]

    child_entries = [
        entry
        for entry in entries
        if entry.name == "hashing_fanout_child_in_tests"
        and parent_entry.id in entry.parent_ids
    ]

    assert len(child_entries) == 50
    assert {entry.id for entry in child_entries} == set(submitted_ids)
    assert {entry.call_path for entry in child_entries} == {
        ".hashing_fanout_parent_in_tests.hashing_fanout_child_in_tests",
    }
    assert {entry.status for entry in child_entries} == {TaskStatus.COMPLETED}
    assert all(entry.parameters.queue == "slurm-queue" for entry in child_entries)
    assert all(entry.parameters.resource == "test" for entry in child_entries)
