import pytest
from simstack.core.node import node
from simstack.models import BinaryOperationInput, FloatData
from simstack.util.importer import import_function


@node
def adder_for_tests(arg: BinaryOperationInput, **kwargs) -> FloatData:
    return FloatData(value=arg.arg1.value + arg.arg2.value)


@pytest.mark.skip(reason="Function comparison fails in pipeline")
@pytest.mark.asyncio
async def test_call_function_by_name(initialized_context):
    # Import the function by name
    function_name = "tests.core.test_run_function_by_name.adder_for_tests"
    func = await import_function(function_name)
    inputs = BinaryOperationInput(arg1=FloatData(value=12), arg2=FloatData(value=5))

    assert func == adder_for_tests
    result = func(inputs)

    assert result.value == 17
