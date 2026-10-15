import inspect
from types import ModuleType
from typing import get_type_hints

import pytest

from simstack.core.node import node
from simstack.core.simstack_result import SimstackResult
from simstack.models import FloatData
from simstack.models.models import NodeModel
from simstack.persistence import eq
from simstack.tables.node_table import CreateNodeTable
from simstack.util.docstring_parser import DocstringParser


@pytest.mark.asyncio
async def test_node_registration_persists_declared_version_for_ui_reruns(repository):
    module = ModuleType("versioned_registration_fixture")
    def versioned(value: FloatData, **kwargs) -> FloatData:
        return value
    versioned.__module__ = module.__name__
    module.versioned = node(version="declared-v2")(versioned)
    await CreateNodeTable(repository)._register_nodes_from_module(module, "")
    registered = await repository.find_one(NodeModel, eq("function_mapping", f"{module.__name__}.versioned"))
    assert registered.version == "declared-v2"


def test_normalize_docstring_type_strips_only_top_level_qualifier():
    assert (
        CreateNodeTable._normalize_docstring_type("FloatData, optional") == "FloatData"
    )
    assert (
        CreateNodeTable._normalize_docstring_type("Dict[str, FloatData], optional")
        == "Dict[str, FloatData]"
    )


def test_build_inputs_with_future_annotations_keeps_resolved_types():
    """Regression: stringified annotations must not wipe input mappings."""
    namespace: dict = {}
    exec(
        """
from __future__ import annotations

from simstack.core.simstack_result import SimstackResult
from simstack.models import FloatData

def sample_node(arg1: FloatData, arg2: FloatData, **kwargs) -> SimstackResult:
    \"\"\"Doc.

    Parameters:
        arg1 (FloatData): First value.
        arg2 (FloatData): Second value.
    \"\"\"
    pass
""",
        namespace,
    )
    func = namespace["sample_node"]
    builder = CreateNodeTable.__new__(CreateNodeTable)
    inputs = builder._build_inputs(
        inspect.signature(func),
        get_type_hints(func),
        DocstringParser(inspect.getdoc(func)).params(),
    )

    assert [(entry["name"], entry["type"]) for entry in inputs] == [
        ("arg1", FloatData),
        ("arg2", FloatData),
    ]


@pytest.mark.asyncio
async def test_build_outputs_accepts_optional_simstack_result_types(repository):
    builder = CreateNodeTable(repository)
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
