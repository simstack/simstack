from unittest.mock import AsyncMock

import pytest

from simstack.models import NodeModel, Parameters
from simstack.tables.node_children import update_node_children


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (ImportError("package unavailable"), "package unavailable"),
        (SystemExit(0), "0"),
    ],
)
async def test_node_child_scan_preserves_model_when_function_import_fails(
    monkeypatch, tmp_path, caplog, failure, message
):
    node_model = NodeModel(
        name="missing_node",
        function_mapping="missing.package.missing_node",
        input_mappings=[],
        default_parameters=Parameters(),
    )

    class FakeDatabase:
        async def find(self, model):
            return [node_model]

        async def delete(self, item):
            raise AssertionError("an import failure must never delete a NodeModel")

        def get_collection(self, model):
            raise AssertionError("the failed model must not be updated")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "simstack.tables.node_children.import_function",
        AsyncMock(side_effect=failure),
    )

    await update_node_children(FakeDatabase(), drops="")

    assert str(node_model.id) in caplog.text
    assert node_model.name in caplog.text
    assert node_model.function_mapping in caplog.text
    assert message in caplog.text
