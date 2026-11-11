from types import SimpleNamespace

import pytest

from simstack.tables import node_children, node_table
from simstack.tables.node_table import make_node_table


def child_node():
    return None


def parent_with_attribute_call():
    """
    Called Nodes:
        child_node
    """
    helper.child_node()


@pytest.mark.asyncio
async def test_make_node_table_runs_second_stage_after_rebuild(monkeypatch, tmp_path):
    from simstack.tables.catalogue import CatalogueCollector
    from unittest.mock import AsyncMock
    second_stage = AsyncMock()
    monkeypatch.setattr(node_table.CreateNodeTable, "second_stage", second_stage)
    await make_node_table(CatalogueCollector(), project_root=tmp_path, ignore_entrypoints=True)
    second_stage.assert_awaited_once_with("")


@pytest.mark.asyncio
async def test_update_node_children_keeps_attribute_calls_from_the_docstring(
    tmp_path, monkeypatch
):
    child = SimpleNamespace(
        id="child",
        name="child_node",
        function_mapping="pkg.child_node",
        called_nodes=[],
    )
    parent = SimpleNamespace(
        id="parent",
        name="parent_node",
        function_mapping="pkg.parent_node",
        description="",
        called_nodes=[],
    )
    from simstack.tables.catalogue import CatalogueCollector
    catalogue = CatalogueCollector(nodes=[child, parent])

    def import_function(function_mapping):
        return parent_with_attribute_call if function_mapping == "pkg.parent_node" else child_node

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(node_children, "_import_callable_from_mapping", import_function)

    await node_children.update_node_children(catalogue, "")

    assert parent.called_nodes == ["pkg.child_node"]
