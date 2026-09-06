import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.services.node_registry_service import (
    apply_persisted_user_editable_fields,
    remember_user_editable_fields,
)
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters


class _FakeDb:
    def __init__(self, persisted: NodeRegistry | None):
        self.persisted = persisted

    async def find_one(self, model, query):
        return self.persisted


def _registry(**overrides) -> NodeRegistry:
    values = {
        "name": "stale-node",
        "status": TaskStatus.RUNNING,
        "function_hash": "function-hash",
        "arg_hash": "arg-hash",
        "func_mapping": "tests.module.function",
        "parameters": Parameters(),
        "custom_name": "original-name",
        "category": "original-category",
    }
    values.update(overrides)
    return NodeRegistry(**values)


@pytest.mark.asyncio
async def test_stale_memory_takes_persisted_ui_fields():
    persisted = _registry(custom_name="ui-name", category="ui-category")
    stale = _registry(id=persisted.id)
    stale.custom_name = "stale-name"
    stale.category = "stale-category"

    await apply_persisted_user_editable_fields(_FakeDb(persisted), stale)

    assert stale.custom_name == "ui-name"
    assert stale.category == "ui-category"


@pytest.mark.asyncio
async def test_local_write_is_kept_when_database_is_unchanged():
    persisted = _registry()
    loaded = _registry(id=persisted.id)
    remember_user_editable_fields(loaded)
    loaded.custom_name = "from-node"
    loaded.category = "from-node-category"

    await apply_persisted_user_editable_fields(_FakeDb(persisted), loaded)

    assert loaded.custom_name == "from-node"
    assert loaded.category == "from-node-category"


@pytest.mark.asyncio
async def test_persisted_ui_write_wins_when_local_value_also_changed():
    persisted = _registry(custom_name="ui-name", category="ui-category")
    loaded = _registry(id=persisted.id)
    remember_user_editable_fields(loaded)
    loaded.custom_name = "from-node"
    loaded.category = "from-node-category"

    await apply_persisted_user_editable_fields(_FakeDb(persisted), loaded)

    assert loaded.custom_name == "ui-name"
    assert loaded.category == "ui-category"


@pytest.mark.asyncio
async def test_apply_skips_when_registry_is_not_yet_persisted():
    entry = _registry()
    remember_user_editable_fields(entry)
    entry.custom_name = "created-name"

    await apply_persisted_user_editable_fields(_FakeDb(None), entry)

    assert entry.custom_name == "created-name"


@pytest.mark.asyncio
async def test_apply_skips_when_registry_has_no_id():
    entry = _registry()
    object.__setattr__(entry, "id", None)
    entry.custom_name = "created-name"

    await apply_persisted_user_editable_fields(_FakeDb(_registry()), entry)

    assert entry.custom_name == "created-name"
    assert entry.id is None
