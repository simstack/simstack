from simstack.core.context import context
from simstack.core.node import node
from simstack.core.resources import allowed_resources
from simstack.models import Parameters, StringData
import pytest
import logging

from simstack.util.project_root_finder import find_project_root


@node
def some_node(arg: StringData, **kwargs) -> StringData:
    return StringData(value=arg.value.lower())


@pytest.mark.local_runner
def test_node_runner(caplog, test_runner):
    allowed_resources.add_resource("test")
    assert allowed_resources.has_resource("test")
    with caplog.at_level(logging.INFO):
        result = some_node(
            StringData(value="Test"), parameters=Parameters(resource="tests")
        )
        assert result.value == "test"
