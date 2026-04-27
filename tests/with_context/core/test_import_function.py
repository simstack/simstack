import pytest
import pytest_asyncio

from simstack.core.node import node

# Add the src directory to the Python path
# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'src')))

from simstack.core.context import context
from simstack.models.models import NodeModel, Parameters
from simstack.models import StringData
from simstack.models.pickle_models import FunctionPickle
from simstack.util.importer import import_function


# Define test functions that will be used for testing import_function
@node
def node_for_testing(value: StringData, **kwargs) -> StringData:
    """A simple test function for testing import_function."""
    return value


@node
def another_node_for_testing(name: StringData, **kwargs) -> StringData:
    """Another test function for testing import_function."""
    return name


@pytest_asyncio.fixture
async def setup_node_model(initialized_context):
    """Set up a NodeModel entry for test_function."""
    # Create a NodeModel entry for test_function
    parameters = Parameters()
    node_model = NodeModel(
        name="test_function",
        function_mapping="tests.core.test_import_function.node_for_testing",
        description="A simple test function",
        input_mappings=[],
        default_parameters=parameters,
    )
    await context.db.save(node_model)

    yield context.db.engine

    # Clean up the node_model
    await context.db.engine.delete(node_model)


@pytest_asyncio.fixture
async def setup_pickled_function(initialized_context):
    """Set up a FunctionPickle entry for another_test_function."""
    # Create a FunctionPickle instance
    function_pickle = FunctionPickle(
        name="another_node_for_testing", module_path="tests.core.test_import_function"
    )

    # Store the function
    function_pickle.store_function(another_node_for_testing)

    # Save the FunctionPickle instance
    await context.db.save(function_pickle)

    # Create a NodeModel entry that references the FunctionPickle
    parameters = Parameters()
    node_model = NodeModel(
        name="another_test_function",
        function_mapping="tests.core.test_import_function.another_node_for_testing",
        description="Another test function",
        input_mappings=[],
        default_parameters=parameters,
        pickle_function=function_pickle,  # Pass the FunctionPickle instance directly
    )
    await context.db.save(node_model)

    yield context.db.engine

    await context.db.delete(node_model)
    await context.db.delete(function_pickle)


@pytest.mark.skip(reason="function comparison works locally but fails in pipeline")
@pytest.mark.asyncio
async def test_import_function_from_node_model(setup_node_model):
    """Test importing a function using NodeModel."""
    # Import the test_function using import_function
    func = await import_function("tests.core.test_import_function.node_for_testing")

    # Verify that the function was imported correctly
    assert func is node_for_testing

    # Call the function and verify it works
    result = func(StringData(value="test"))
    assert result.value == "test"


@pytest.mark.skip(reason="pickle function tests must be fixed")
@pytest.mark.asyncio
async def test_import_function_from_pickle(setup_pickled_function):
    """Test importing a function from a FunctionPickle."""
    # Import the another_test_function using import_function
    func = await import_function(
        "tests.core.test_import_function.another_node_for_testing"
    )

    # Verify that the function was imported correctly
    assert func.__name__ == "another_node_for_testing"

    # Call the function and verify it works
    result = func(StringData(value="test"))
    assert result.value == "test"


@pytest.mark.asyncio
async def test_import_function_nonexistent():
    """Test importing a non-existent function which should raise ImportError."""
    # Try to import a non-existent function and expect ImportError

    with pytest.raises(
        LookupError,
        match="Function tests.core.test_import_function.nonexistent_function not found in the NodeModel Table",
    ):
        await import_function("tests.core.test_import_function.nonexistent_function")


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
