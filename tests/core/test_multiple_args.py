from simstack.core.node import node
from simstack.models import Parameters, IntData


@node
def adder_two_args(arg1: IntData, arg2: IntData, **kwargs) -> IntData:
    return IntData(value=arg1.value + arg2.value)


def test_adder():
    parameters = Parameters(force_rerun=True)
    result = adder_two_args(IntData(value=1), IntData(value=2), parameters=parameters)
    assert result.value == 3


def test_adder_kwarg():
    parameters = Parameters(force_rerun=True)
    result = adder_two_args(
        IntData(value=1), arg2=IntData(value=2), parameters=parameters
    )
    assert result.value == 3
