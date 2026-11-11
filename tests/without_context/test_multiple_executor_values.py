from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import simstack.methods.multiple_executor as executor_module
from simstack.models import FloatData
from simstack.server_api.http_transport import RunnerAPIError


@pytest.mark.asyncio
async def test_executor_keeps_local_results_for_pure_iteration_and_publication(monkeypatch):
    async def calculate(value, **kwargs):
        return FloatData(value=value.value + 1)

    monkeypatch.setattr(executor_module, "import_function", AsyncMock(return_value=calculate))
    monkeypatch.setattr(executor_module, "context", SimpleNamespace(server=SimpleNamespace(values=object())))
    source = executor_module.MultipleExecutorInput(function_mapping="workflow.calculate",
        input_mappings=["simstack.models.base_types.FloatData"], result_mapping="simstack.models.base_types.FloatData")
    await source.append(FloatData(value=2))
    output = await source.execute()
    results = [value async for value in output.result_generator()]
    assert [value.value for value in results] == [3]
    assert output.result_ids == [results[0].id]
    assert output.custom_model_dump()["result"][0]["value"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("during_import", [True, False])
async def test_executor_propagates_server_failure_without_partial_output(monkeypatch, during_import):
    failure = RunnerAPIError("Server unavailable")

    async def calculate(value, **kwargs):
        raise failure

    importer = AsyncMock(side_effect=failure) if during_import else AsyncMock(return_value=calculate)
    monkeypatch.setattr(executor_module, "import_function", importer)
    monkeypatch.setattr(executor_module, "context", SimpleNamespace(server=SimpleNamespace(values=object())))
    source = executor_module.MultipleExecutorInput(function_mapping="workflow.calculate",
        input_mappings=["simstack.models.base_types.FloatData"], result_mapping="simstack.models.base_types.FloatData")
    await source.append(FloatData(value=2))
    with pytest.raises(RunnerAPIError) as raised:
        await source.execute()
    assert raised.value is failure
