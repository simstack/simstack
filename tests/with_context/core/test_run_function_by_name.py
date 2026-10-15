import pytest
from simstack.core.node import node
from simstack.models import BinaryOperationInput, FloatData
from simstack.util.importer import import_function


@node
def adder_for_tests(arg: BinaryOperationInput, **kwargs) -> FloatData:
    return FloatData(value=arg.arg1.value + arg.arg2.value)


@pytest.mark.asyncio
async def test_call_function_by_canonical_path(repository):
    function_path = f"{__name__}.adder_for_tests"
    func = await import_function(function_path, repository)
    inputs = BinaryOperationInput(arg1=FloatData(value=12), arg2=FloatData(value=5))

    result = func(inputs)

    assert result.value == 17
