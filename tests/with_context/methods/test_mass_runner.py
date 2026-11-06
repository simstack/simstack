import asyncio
from unittest.mock import AsyncMock

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.node_runner import NodeRunner
from simstack.methods.mass_runner import MassRunner
from simstack.models import DataSet, FloatData
from simstack.runner_api.transport import RunnerAPIError


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


@pytest.mark.asyncio
async def test_api_failure_propagates_after_siblings_finish(monkeypatch):
    sibling_started = asyncio.Event()
    api_failed = asyncio.Event()
    finish_sibling = asyncio.Event()
    sibling_finished = False
    sibling_cancelled = False
    calls: list[float] = []
    failure = RunnerAPIError("Status query failed after safe-read retries")

    async def calculate(value: FloatData, **kwargs):
        nonlocal sibling_finished, sibling_cancelled
        calls.append(value.value)
        if value.value == 1.0:
            await sibling_started.wait()
            api_failed.set()
            raise failure
        sibling_started.set()
        try:
            await finish_sibling.wait()
        except asyncio.CancelledError:
            sibling_cancelled = True
            raise
        sibling_finished = True
        return FloatData(value=value.value)

    runner = _runner("mass_runner_api_failure", calculate)
    save = AsyncMock()
    monkeypatch.setattr(DataSet, "save", save)

    async def run():
        async with runner:
            runner.create_tasks(FloatData(value=1.0))
            runner.create_tasks(FloatData(value=1.0))
            runner.create_tasks(FloatData(value=2.0))

    workflow = asyncio.create_task(run())
    try:
        await asyncio.wait_for(api_failed.wait(), timeout=5)
        await asyncio.sleep(0)
        assert not workflow.done()
        finish_sibling.set()
        with pytest.raises(RunnerAPIError) as raised:
            await workflow
        assert raised.value is failure
    finally:
        finish_sibling.set()
        await asyncio.gather(workflow, return_exceptions=True)

    assert sorted(calls) == [1.0, 2.0]
    assert sibling_finished and not sibling_cancelled
    assert all(task.done() and not task.cancelled() for task in runner._tasks)
    assert runner._tasks[0].exception() is failure
    assert runner._tasks[1].exception() is failure
    rows = list(runner.dataset["tasks"].values())
    assert len(rows) == 1
    assert rows[0]["arg_value"].value == 2.0
    assert rows[0]["success"].value is True
    assert runner.status != TaskStatus.COMPLETED
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_wrapper_failure_is_propagated(monkeypatch):
    async def calculate(value: FloatData, **kwargs):
        return value

    runner = _runner("mass_runner_wrapper_failure", calculate)
    failure = ValueError("Previous dataset could not be read")
    monkeypatch.setattr(
        runner, "recover_orphaned_datasets", AsyncMock(side_effect=failure)
    )
    save = AsyncMock()
    monkeypatch.setattr(DataSet, "save", save)

    with pytest.raises(ValueError) as raised:
        async with runner:
            runner.create_tasks(FloatData(value=1.0))
            runner.create_tasks(FloatData(value=2.0))

    assert raised.value is failure
    assert all(task.done() for task in runner._tasks)
    assert runner.status != TaskStatus.COMPLETED
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RunnerAPIError, ValueError])
async def test_context_body_failure_is_not_suppressed(monkeypatch, error_type):
    async def calculate(value: FloatData, **kwargs):
        return value

    runner = _runner("mass_runner_body_failure", calculate)
    save = AsyncMock()
    monkeypatch.setattr(DataSet, "save", save)
    failure = error_type("Input query failed")

    with pytest.raises(error_type) as raised:
        async with runner:
            runner.create_tasks(FloatData(value=1.0))
            raise failure

    assert raised.value is failure
    assert all(task.done() and not task.cancelled() for task in runner._tasks)
    assert runner.status != TaskStatus.COMPLETED
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_body_cancellation_takes_priority_over_child_api_failure(monkeypatch):
    failure = RunnerAPIError("Status query failed")
    cancellation = asyncio.CancelledError("Parent was cancelled")
    sibling_finished = False

    async def calculate(value: FloatData, **kwargs):
        nonlocal sibling_finished
        if value.value == 1.0:
            raise failure
        await asyncio.sleep(0)
        sibling_finished = True
        return value

    runner = _runner("mass_runner_cancelled_body", calculate)
    save = AsyncMock()
    monkeypatch.setattr(DataSet, "save", save)

    with pytest.raises(asyncio.CancelledError) as raised:
        async with runner:
            runner.create_tasks(FloatData(value=1.0))
            runner.create_tasks(FloatData(value=2.0))
            raise cancellation

    assert raised.value is cancellation
    assert sibling_finished
    assert all(task.done() and not task.cancelled() for task in runner._tasks)
    assert runner._tasks[0].exception() is failure
    assert runner.status != TaskStatus.COMPLETED
    save.assert_not_awaited()
