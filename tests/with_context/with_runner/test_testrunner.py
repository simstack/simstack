from simstack.core.node import node
from simstack.core.resources import allowed_resources
from simstack.models import Parameters, StringData
import pytest
import logging


@node
def some_node(arg: StringData, **kwargs) -> StringData:
    return StringData(value=arg.value.lower())


@pytest.mark.local_runner
@pytest.mark.runner_smoke
def test_node_runner(caplog):
    allowed_resources.add_resource("test")
    assert allowed_resources.has_resource("test")
    with caplog.at_level(logging.INFO):
        result = some_node(
            StringData(value="Test"), parameters=Parameters(resource="test")
        )
        assert result.value == "test"
