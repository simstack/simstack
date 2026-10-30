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
async def test_make_node_table_runs_second_stage_after_rebuild(monkeypatch):
    calls = []

    class FakeCreator:
        def __init__(self, db, write_schema=False, project_root=None):
            self.db = db

        async def build(self, **kwargs):
            calls.append(("build", kwargs))

        async def second_stage(self, drops):
            calls.append(("second", drops))

    monkeypatch.setattr(node_table, "CreateNodeTable", FakeCreator)

    await make_node_table(object())

    assert calls[0][0] == "build"
    assert calls[0][1]["drops"] == ""
    assert calls[1] == ("second", "")


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
    updates = []

    class FakeCollection:
        async def update_one(self, query, update):
            updates.append((query, update))

    class FakeDatabase:
        async def find(self, model):
            return [child, parent]

        def get_collection(self, model):
            return FakeCollection()

    async def import_function(function_mapping, database):
        if function_mapping == "pkg.parent_node":
            return parent_with_attribute_call
        return child_node

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(node_children, "import_function", import_function)

    await node_children.update_node_children(FakeDatabase(), "")

    parent_update = next(update for query, update in updates if query == {"_id": "parent"})
    assert parent_update == {"$set": {"called_nodes": ["pkg.child_node"]}}
