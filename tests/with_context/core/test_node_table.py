import inspect
from typing import Optional, Union, get_type_hints

import pytest

from simstack.core.context import context
from simstack.core.simstack_result import SimstackResult
from simstack.models import FloatData
from simstack.tables.node_table import CreateNodeTable
from simstack.util.docstring_parser import DocstringParser


def test_normalize_docstring_type_strips_only_top_level_qualifier():
    assert (
        CreateNodeTable._normalize_docstring_type("FloatData, optional") == "FloatData"
    )
    assert (
        CreateNodeTable._normalize_docstring_type("Dict[str, FloatData], optional")
        == "Dict[str, FloatData]"
    )


@pytest.mark.asyncio
async def test_build_outputs_accepts_optional_simstack_result_types():
    builder = CreateNodeTable(context.db.engine)
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


def test_get_class_mapping_unwraps_pep604_optional_types():
    builder = CreateNodeTable(context.db.engine)
    expected_mapping = builder.get_class_mapping(FloatData, "src")

    assert builder.get_class_mapping(FloatData | None, "src") == expected_mapping


def test_get_class_mapping_unwraps_typing_optional_types():
    builder = CreateNodeTable(context.db.engine)
    expected_mapping = builder.get_class_mapping(FloatData, "src")

    assert builder.get_class_mapping(Optional[FloatData], "src") == expected_mapping
    assert builder.get_class_mapping(Union[FloatData, None], "src") == expected_mapping


def test_build_inputs_accepts_pep604_optional_annotations():
    def node_input(value: FloatData | None) -> FloatData:
        return value or FloatData(value=0)

    builder = CreateNodeTable(context.db.engine)
    inputs = builder._build_inputs(
        inspect.signature(node_input),
        get_type_hints(node_input),
        doc_params=None,
    )

    assert inputs[0]["name"] == "value"
    assert inputs[0]["type"] == FloatData | None


@pytest.mark.asyncio
async def test_resolve_input_mappings_accepts_optional_annotations():
    builder = CreateNodeTable(context.db.engine)
    expected_mapping = builder.get_class_mapping(FloatData, "src")

    input_mappings = await builder._resolve_input_mappings(
        "optional_node",
        [
            {"name": "pep604_value", "type": FloatData | None},
            {"name": "typing_value", "type": Optional[FloatData]},
        ],
        "src",
    )

    assert input_mappings == [expected_mapping, expected_mapping]


@pytest.mark.asyncio
async def test_build_outputs_accepts_optional_return_annotations():
    builder = CreateNodeTable(context.db.engine)
    expected_mapping = builder.get_class_mapping(FloatData, "src")
    parser = DocstringParser("")

    pep604_mappings = await builder._build_outputs(
        "pep604_return_node",
        {"return": FloatData | None},
        parser,
        "src",
    )
    typing_mappings = await builder._build_outputs(
        "typing_return_node",
        {"return": Optional[FloatData]},
        parser,
        "src",
    )

    assert [(result.name, result.mapping) for result in pep604_mappings] == [
        ("result", expected_mapping),
    ]
    assert [(result.name, result.mapping) for result in typing_mappings] == [
        ("result", expected_mapping),
    ]
