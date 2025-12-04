from simstack.core.node import node
from simstack.core.resources import allowed_resources
from simstack.models import Parameters, StringData
import pytest
import logging


@node
def some_node(arg: StringData, **kwargs) -> StringData:
    return StringData(value=arg.value.lower())


@pytest.mark.skip(reason="works locally but not in gitlab ci/cd")
@pytest.mark.local_runner
def test_node_runner(caplog, test_runner):
    assert allowed_resources.has_resource("tests")
    with caplog.at_level(logging.INFO):
        result = some_node(
            StringData(value="Test"), parameters=Parameters(resource="tests")
        )
        assert result.value == "test"
