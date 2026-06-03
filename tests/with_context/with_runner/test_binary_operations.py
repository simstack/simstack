import pytest

from simstack.core.node import node
from simstack.models import FloatData, Parameters


@node
def simstack_test_adder(arg1: FloatData, arg2: FloatData, **kwargs) -> FloatData:
    return FloatData(value=arg1.value + arg2.value)


@pytest.mark.asyncio
@pytest.mark.local_runner
@pytest.mark.runner_smoke
def test_adder():
    parameters = Parameters(resource="test",force_rerun=True)
    result = simstack_test_adder(FloatData(value=1), FloatData(value=2), parameters=parameters)
    assert result.value == 3
    assert isinstance(result, FloatData)
