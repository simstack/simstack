"""Keep UI-edited NodeRegistry fields intact across delayed saves.

A registry entry is often kept in memory for the life of a node run. In that
window the UI may change ``custom_name`` and ``category``. A later full-document
save of the stale object would overwrite those edits unless the persisted
values are loaded and merged first.
"""

from __future__ import annotations

from typing import Any

from simstack.models.node_registry import NodeRegistry
from simstack.persistence import Repository, eq

USER_EDITABLE_NODE_REGISTRY_FIELDS = ("custom_name", "category")
_SNAPSHOT_ATTR = "_user_editable_fields_snapshot"


def remember_user_editable_fields(registry_entry: NodeRegistry) -> None:
    snapshot = {
        name: getattr(registry_entry, name)
        for name in USER_EDITABLE_NODE_REGISTRY_FIELDS
    }
    object.__setattr__(registry_entry, _SNAPSHOT_ATTR, snapshot)


async def apply_persisted_user_editable_fields(
    repository: Repository, registry_entry: NodeRegistry
) -> None:
    """Copy UI-editable fields from the database onto ``registry_entry``.

    Local writes (for example ``SimstackResult.custom_name``) are kept when the
    database still has the values from when this object was loaded or last
    saved. If both this object and the database changed a field, the database
    value wins so a UI edit is not discarded.
    """
    registry_id = getattr(registry_entry, "id", None)
    if registry_id is None:
        return

    persisted = await repository.find_one(NodeRegistry, eq("id", registry_id))
    if persisted is None:
        return

    snapshot = getattr(registry_entry, _SNAPSHOT_ATTR, None)
    for name in USER_EDITABLE_NODE_REGISTRY_FIELDS:
        local_value = getattr(registry_entry, name)
        persisted_value = getattr(persisted, name)
        persisted_wins = (
            snapshot is None
            or local_value == persisted_value
            or local_value == snapshot[name]
            or persisted_value != snapshot[name]
        )
        if persisted_wins:
            setattr(registry_entry, name, persisted_value)
