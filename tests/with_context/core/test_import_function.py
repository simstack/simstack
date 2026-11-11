import pytest
from simstack.core.context import context
from simstack.core.node import node
from simstack.models import StringData
from simstack.util.importer import import_function


@node
def node_for_testing(value: StringData, **kwargs) -> StringData:
    """Return the input value.

    Parameters:
        value (StringData): Input value.
    """
    return value


@node
def another_node_for_testing(name: StringData, **kwargs) -> StringData:
    return name


@pytest.mark.asyncio
async def test_import_function_from_node_model(initialized_context):
    func = await import_function(f"{__name__}.node_for_testing", context.server.values)
    assert func is node_for_testing
    result = func._inner(StringData(value="test"))
    assert result.value == "test"


@pytest.mark.asyncio
async def test_import_function_nonexistent(initialized_context):
    with pytest.raises(LookupError, match="nonexistent_function.*revision catalogue"):
        await import_function(f"{__name__}.nonexistent_function", context.server.values)
