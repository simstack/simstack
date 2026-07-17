import pytest
import pytest_asyncio
from simstack.core.context import context
from simstack.models.parameters import Parameters
from simstack.models.node_registry import NodeRegistry


@pytest_asyncio.fixture
async def node_registry():
    """Create a properly configured NodeRegistry instance for testing."""
    # Create a proper Parameters instance
    parameters = Parameters()

    node_data = NodeRegistry(
        name="test_node",
        status="completed",  # Use valid TaskStatus value
        input_ids=[],
        result_ids=[],
        function_hash="test_function_hash",  # Required field
        arg_hash="test_arg_hash",  # Required field
        func_mapping="test.module.function",  # Required field
        parameters=parameters,  # Required field
    )

    await context.db.save(node_data)
    
    yield node_data
    
    try:
        await context.db.delete(node_data)
    except Exception:
        pass
