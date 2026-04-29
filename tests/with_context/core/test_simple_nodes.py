import pytest

from simstack.core.context import context
from simstack.models import FloatData, BinaryOperationInput, IteratorInput
from simstack.core.node import node
from simstack.models.parameters import SlurmParameters


@node()
def adder_in_tests(args: BinaryOperationInput, **kwargs) -> FloatData:
    return FloatData(value=args.arg1.value + args.arg2.value)


@node()
def use_adder_in_tests(args: BinaryOperationInput, **kwargs) -> FloatData:
    parent_id = kwargs.get("parent_id", None)
    result = adder_in_tests(args, parent_id=parent_id)
    return FloatData(value=result.value)


@node()
def multiply_in_tests(args: BinaryOperationInput, **kwargs) -> FloatData:
    return FloatData(value=args.arg1.value * args.arg2.value)


@node()
def add_multiply_in_tests(args: BinaryOperationInput, **kwargs) -> FloatData:
    add_result = adder_in_tests(args)
    multiply_result = multiply_in_tests(
        BinaryOperationInput(arg1=add_result, arg2=args.arg2)
    )
    return FloatData(value=multiply_result.value)


@node()
def iterator_workflow_explicit_in_tests(args: IteratorInput, **kwargs) -> FloatData:
    def generator(start, stop):
        for i in range(start, stop):
            yield i

    results_table = [
        adder_in_tests(
            BinaryOperationInput(
                arg1=FloatData(value=2), arg2=FloatData(value=float(value))
            )
        )
        for value in generator(args.start, args.stop)
    ]
    if any([result.value is None for result in results_table]):
        raise RuntimeError("Generator yielded None")
    result = sum([result.value for result in results_table])
    return FloatData(value=result)


@node()
def iterator_workflow_in_tests(args: IteratorInput, **kwargs) -> FloatData:
    try:
        generator_func = eval(args.generator)
    except Exception as e:
        raise ValueError(f"Invalid generator expression: {e}")

    results_table = [
        adder_in_tests(
            BinaryOperationInput(
                arg1=FloatData(value=2), arg2=FloatData(value=float(value))
            )
        )
        for value in generator_func(args.start, args.stop)
    ]
    if any([result.value is None for result in results_table]):
        raise RuntimeError("Generator yielded None")
    result = sum([result.value for result in results_table])
    return FloatData(value=result)


@node(
    resource="local",
    queue="slurm-queue",
    slurm_parameters=SlurmParameters(
        nodes=3,
        tasks=None,
        tasks_per_node=2,
        cpus_per_task=4,
        mem="9G",
        time="03:00:00",
    ),
    force_rerun=True,
    recompute_artifacts=True,
)
def legacy_decorator_parameters_in_tests(
    args: BinaryOperationInput, **kwargs
) -> FloatData:
    return FloatData(value=args.arg1.value)


def test_adder():
    assert context.initialized is True
    result = adder_in_tests(
        BinaryOperationInput(
            type="adder", arg1=FloatData(value=1), arg2=FloatData(value=2)
        )
    )
    assert result.value == pytest.approx(3)


def test_wrapping():
    # a single node being wrapped in another node
    result2 = add_multiply_in_tests(
        BinaryOperationInput(arg1=FloatData(value=6), arg2=FloatData(value=2))
    )
    assert result2.value == pytest.approx(16)


def test_iterator_workflow():
    result3 = iterator_workflow_in_tests(
        IteratorInput(start=1, stop=3, generator="range")
    )
    assert result3.value == pytest.approx(7)


def test_add_multiply():
    result = add_multiply_in_tests(
        BinaryOperationInput(arg1=FloatData(value=6), arg2=FloatData(value=2))
    )
    assert result.value == pytest.approx(16)


def test_call_path():
    result = use_adder_in_tests(
        BinaryOperationInput(
            arg1=FloatData(name="a", value=12212),
            arg2=FloatData(name="b", value=123123),
        )
    )
    assert result.value == 135335, f"Expected 135335, got {result}"


def test_legacy_decorator_parameter_kwargs_are_applied_to_parameters():
    parameters = legacy_decorator_parameters_in_tests._node_parameters

    assert parameters.resource == "local"
    assert parameters.queue == "slurm-queue"
    assert parameters.force_rerun is True
    assert parameters.recompute_artifacts is True
    assert parameters.slurm_parameters.nodes == 3
    assert parameters.slurm_parameters.tasks is None
    assert parameters.slurm_parameters.tasks_per_node == 2
    assert parameters.slurm_parameters.cpus_per_task == 4
    assert parameters.slurm_parameters.mem == "9G"
    assert parameters.slurm_parameters.time == "03:00:00"
