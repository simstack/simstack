import pytest

from simstack.core.context import context
from simstack.core.simstack_result import SimstackResult
from simstack.models import FloatData
from simstack.tables.node_table import CreateNodeTable
from simstack.util.docstring_parser import DocstringParser


@pytest.mark.asyncio
async def test_build_outputs_accepts_optional_simstack_result_types():
    builder = CreateNodeTable(context.db)
    expected_mapping = builder.get_class_mapping(FloatData, "src")
    parser = DocstringParser(
        """
        SimstackResult:
            result1 (FloatData, optional): Primary result.
            result2 (Dict[str, FloatData], optional): Secondary indexed result.
        """
    )

    result_mappings = await builder._build_outputs(
        "test_node",
        {"return": SimstackResult},
        parser,
        "src",
    )

    assert [(result.name, result.mapping) for result in result_mappings] == [
        ("result1", expected_mapping),
        ("result2", f"Dict[str,{expected_mapping}]"),
    ]
