import asyncio
from unittest.mock import AsyncMock

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.context import context
from simstack.core.node_runner import NodeRunner
from simstack.methods.mass_runner import MassRunner
from simstack.models import DataSet, FloatData
from simstack.server_api.http_transport import RunnerAPIError


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
async def test_dataset_member_snapshots_get_their_own_calculation():
    from simstack.models.dataset_metadata import DataSetMetadata
    from simstack.models.datasettuple import DataSetTuple, DataSetTupleSection

    member = FloatData(value=1.0)
    section = DataSetTupleSection()
    section.extend([(member,)])
    dataset = await context.server.values.publish_value(DataSetTuple(
        metadata=DataSetMetadata(field_name="mass_runner_member_inputs"),
        sections={"rows": section},
    ))
    original = await context.server.values.get_value(DataSetTuple, dataset.id)
    edited = await context.server.values.get_value(FloatData, member.id)
    edited.value = 2.0
    await context.server.values.publish_value(edited)
    current = await context.server.values.get_value(DataSetTuple, dataset.id)
    calls = []

    async def calculate(sample, **kwargs):
        value = sample.sections["rows"][0][0].value
        calls.append(value)
        return FloatData(value=value * 10)

    async with _runner("mass_runner_member_snapshots", calculate) as runner:
        runner.create_tasks(original)
        runner.create_tasks(current)
        runner.create_tasks(original)

    assert sorted(calls) == [1.0, 2.0]
    assert [
        (row["arg_sample"].sections["rows"][0][0].value,
         row["result_FloatData"].value)
        for row in runner.dataset["tasks"].values()
    ] == [(1.0, 10.0), (2.0, 20.0), (1.0, 10.0)]


@pytest.mark.asyncio
async def test_duplicate_files_use_content_identity():
    from simstack.models.files import FileStack

    calls = []

    async def calculate(source, **kwargs):
        calls.append(source.name)
        return FloatData(value=source.size)

    inputs = [
        FileStack.from_string("same", "first.txt"),
        FileStack.from_string("same", "second.txt"),
        FileStack.from_string("different", "third.txt"),
    ]
    async with _runner("mass_runner_file_contents", calculate) as runner:
        for source in inputs:
            runner.create_tasks(source)

    assert calls == ["first.txt", "third.txt"]
    assert sorted(
        (row["arg_source"].name, row["result_FloatData"].value)
        for row in runner.dataset["tasks"].values()
    ) == [("first.txt", 4.0), ("second.txt", 4.0), ("third.txt", 9.0)]


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
    publication = AsyncMock(wraps=context.server.values.publish_value)
    monkeypatch.setattr(context.server.values, "publish_value", publication)

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
    assert not any(isinstance(call.args[0], DataSet) for call in publication.await_args_list)


@pytest.mark.asyncio
async def test_unexpected_wrapper_failure_is_propagated(monkeypatch):
    async def calculate(value: FloatData, **kwargs):
        return value

    runner = _runner("mass_runner_wrapper_failure", calculate)
    failure = ValueError("Previous dataset could not be read")
    monkeypatch.setattr(
        runner, "recover_orphaned_datasets", AsyncMock(side_effect=failure)
    )
    publication = AsyncMock(wraps=context.server.values.publish_value)
    monkeypatch.setattr(context.server.values, "publish_value", publication)

    with pytest.raises(ValueError) as raised:
        async with runner:
            runner.create_tasks(FloatData(value=1.0))
            runner.create_tasks(FloatData(value=2.0))

    assert raised.value is failure
    assert all(task.done() for task in runner._tasks)
    assert runner.status != TaskStatus.COMPLETED
    assert not any(isinstance(call.args[0], DataSet) for call in publication.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RunnerAPIError, ValueError])
async def test_context_body_failure_is_not_suppressed(monkeypatch, error_type):
    async def calculate(value: FloatData, **kwargs):
        return value

    runner = _runner("mass_runner_body_failure", calculate)
    publication = AsyncMock(wraps=context.server.values.publish_value)
    monkeypatch.setattr(context.server.values, "publish_value", publication)
    failure = error_type("Input query failed")

    with pytest.raises(error_type) as raised:
        async with runner:
            runner.create_tasks(FloatData(value=1.0))
            raise failure

    assert raised.value is failure
    assert all(task.done() and not task.cancelled() for task in runner._tasks)
    assert runner.status != TaskStatus.COMPLETED
    assert not any(isinstance(call.args[0], DataSet) for call in publication.await_args_list)


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
    publication = AsyncMock(wraps=context.server.values.publish_value)
    monkeypatch.setattr(context.server.values, "publish_value", publication)

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
    assert not any(isinstance(call.args[0], DataSet) for call in publication.await_args_list)
