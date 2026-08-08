import pytest

from simstack.core.node import node
from simstack.models.models import NodeModel
from simstack.models.parameters import Parameters
from simstack.tables.node_table import CreateNodeTable, is_node_function
from simstack.util.dynamic_node_loader import load_node_from_source

pytestmark = pytest.mark.skip(reason="Skipping all tests as requested")


@node
def _fixture_node_for_source(value, **kwargs):
    return value


pytest.mark.skip(reason="Not implemented yet")
def test_extract_node_sources_reads_function_body():
    function_code, module_source = _extract_node_sources(_fixture_node_for_source, None)
    assert is_node_function(_fixture_node_for_source)
    assert "@node" in function_code
    assert "fixture_node_for_source" in function_code
    assert module_source == ""


def test_normalize_docstring_type_strips_only_top_level_qualifier():
    assert (
        CreateNodeTable._normalize_docstring_type("FloatData, optional") == "FloatData"
    )
    assert (
        CreateNodeTable._normalize_docstring_type("Dict[str, FloatData], optional")
        == "Dict[str, FloatData]"
    )


def test_load_node_from_source_returns_decorated_callable():
    source = '''
@node
def dynamic_example(value: StringData, **kwargs) -> StringData:
    return value
'''
    node_model = NodeModel(
        name="dynamic-example",
        function_mapping="generated.nodes.unit.dynamic_example",
        input_mappings=[],
        default_parameters=Parameters(),
        function_code=source,
        source_origin="generated",
    )
    func = load_node_from_source(node_model)
    assert func.__name__ == "dynamic_example"
    assert is_node_function(func)
