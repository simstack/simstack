import logging
import pytest

from tests.core.test_simple_nodes import adder_in_tests
from simstack.core.simstack_result import SimstackResult
from simstack.core.node import node
from simstack.core.node_runner import NodeRunner
from simstack.models import BinaryOperationInput, FloatData
from simstack.models.files import FileStack

logger = logging.getLogger("TestNode")


@node
def node_with_simstack_results(arg: BinaryOperationInput, **kwargs) -> SimstackResult:
    node_runner = NodeRunner(name="test_node", logger=logger)
    result1 = FloatData(value=arg.arg1.value + arg.arg2.value)
    result2 = FloatData(value=arg.arg1.value * arg.arg2.value)
    node_runner.info(f"Computed results: {result1.value}, {result2.value}")
    node_runner.result1 = result1
    node_runner.result2 = result2
    return node_runner.succeed()


@node
def node_with_files(
    arg: BinaryOperationInput, test_file: FileStack, **kwargs
) -> SimstackResult:
    node_runner = NodeRunner(name="test_node", logger=logger)
    result1 = FloatData(value=arg.arg1.value + arg.arg2.value)
    result2 = FloatData(value=arg.arg1.value * arg.arg2.value)
    node_runner.info(f"Computed results: {result1.value}, {result2.value}")
    node_runner.result1 = result1
    node_runner.result2 = result2
    node_runner.files.append(test_file)

    return node_runner.succeed()


def test_loading_results():
    arg = BinaryOperationInput(arg1=FloatData(value=55651), arg2=FloatData(value=55665))
    result1 = adder_in_tests(arg)
    result2 = adder_in_tests(arg)

    assert result1 == result2


def test_return_simstack_results(caplog):
    with caplog.at_level(logging.INFO):
        arg = BinaryOperationInput(arg1=FloatData(value=123), arg2=FloatData(value=456))
        result = node_with_simstack_results(arg)

        assert isinstance(result, SimstackResult)
        assert result.result1.value == 579
        assert result.result2.value == 56088
        assert result.status == "completed"

        # Check if the results are correctly logged
        assert "Computed results: 579.0" in caplog.text

        caplog.clear()  # Clear logs for the next test

        result_rerun = node_with_simstack_results(arg)
        assert isinstance(result_rerun, SimstackResult)
        assert result_rerun.result1.value == 579
        assert result_rerun.result2.value == 56088


@pytest.mark.asyncio
async def test_node_with_files(caplog, test_file_stack):
    """Test the node_with_files function to ensure it processes files correctly."""
    with caplog.at_level(logging.INFO):
        # Create test arguments
        arg = BinaryOperationInput(arg1=FloatData(value=200), arg2=FloatData(value=300))

        # Call the node function with the fixture file
        result = node_with_files(arg, test_file_stack)

        # Verify the result
        assert isinstance(result, SimstackResult)
        assert result.result1.value == 500
        assert result.result2.value == 60000
        assert result.status == "completed"

        # Check that files are included in the result
        assert len(result.files) == 1
        assert result.files[0] == test_file_stack

        # Verify the file name is preserved
        assert test_file_stack.name == "test_file.txt"

        # Check if the results are correctly logged
        assert "Computed results: 500.0, 60000.0" in caplog.text

        caplog.clear()

        # Test rerun to ensure file handling is consistent
        result_rerun = node_with_files(arg, test_file_stack)
        assert isinstance(result_rerun, SimstackResult)
        assert result_rerun.result1.value == 500
        assert result_rerun.result2.value == 60000
        assert len(result_rerun.files) == 1


@pytest.mark.asyncio
async def test_node_with_files_multiple_operations(caplog, test_file_stack):
    """Test node_with_files with different input values."""
    with caplog.at_level(logging.INFO):
        # Test with different arguments
        arg = BinaryOperationInput(arg1=FloatData(value=10), arg2=FloatData(value=5))
        result1 = node_with_files(arg, test_file_stack)

        assert isinstance(result1, SimstackResult)
        assert result1.result1.value == 15
        assert result1.result2.value == 50
        assert result1.status == "completed"
        assert len(result1.files) == 1

        # Verify the file content is accessible
        assert test_file_stack.name == "test_file.txt"

        # Check logging
        assert "Computed results: 15.0, 50.0" in caplog.text

        result2 = node_with_files(arg, test_file_stack)
        assert result2.result1.value == 15
        assert result2.result2.value == 50
        assert result2.status == "completed"
        assert len(result2.files) == 1
