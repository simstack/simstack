import asyncio

import pytest

from simstack.core.node_runner import NodeRunner
from simstack.methods.mass_runner import MassRunner
from simstack.models import FloatData


def _runner(name: str, node) -> MassRunner:
    parent = NodeRunner(name, f"{name}-task")
    return MassRunner(node, max_concurrency=2, node_runner=parent, arg_hash="parent-hash")


@pytest.mark.asyncio
async def test_duplicate_arg_hash_reuses_one_result_per_input():
    calls: list[float] = []

    async def echo(value: FloatData, **kwargs):
        calls.append(value.value)
        await asyncio.sleep(0.05)
        return FloatData(field_name="echoed", value=value.value + 10)

    inputs = [
        FloatData(value=1.0),
        FloatData(value=1.0),
        FloatData(value=2.0),
        FloatData(value=1.0),
    ]
    async with _runner("mass_runner_duplicate_hash", echo) as runner:
        for item in inputs:
            runner.create_tasks(item)

    assert sorted(calls) == [1.0, 2.0]
    section = runner.dataset["tasks"]
    assert len(section) == 4
    stored_ids = []
    for row in section.values():
        stored_ids.append(row["arg_value"].id)
        assert row["success"].value is True
        assert row["result_FloatData"].value == row["arg_value"].value + 10
    assert sorted(stored_ids) == sorted(item.id for item in inputs)
    assert sum("#" in name for name in section.keys()) == 2


@pytest.mark.asyncio
async def test_duplicate_arg_hash_records_primary_failure():
    calls: list[float] = []

    async def boom(value: FloatData, **kwargs):
        calls.append(value.value)
        raise RuntimeError("dftb failed")

    inputs = [FloatData(value=3.0), FloatData(value=3.0)]
    async with _runner("mass_runner_duplicate_failure", boom) as runner:
        for item in inputs:
            runner.create_tasks(item)

    assert calls == [3.0]
    section = runner.dataset["tasks"]
    assert len(section) == 2
    for row in section.values():
        assert row["success"].value is False
        assert "dftb failed" in row["error"].value
    assert sum("#" in name for name in section.keys()) == 1
