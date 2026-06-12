"""Audit StringDataList documents created by ObjectListMixin branches.

Why this exists:
- `StringDataList` is a new persisted model introduced by ObjectListMixin work
- for clean `main` -> branch rollouts there is usually nothing to migrate
- if some user databases already contain branch-created StringDataList docs, we
  still want a quick integrity check before rollout

What it does:
- scans `string_data_list`
- verifies that `elements` only contains ObjectIds
- verifies that referenced `string_data` documents exist

This script is intentionally an audit, not a mutating migration.
"""

from __future__ import annotations

import asyncio

from bson import ObjectId

from _mongo_migration_utils import connect_database, get_logger


async def migrate():
    logger = get_logger("audit_string_data_lists")
    client, db = await connect_database()
    try:
        invalid_lists = 0
        missing_refs = 0
        async for document in db["string_data_list"].find({}):
            elements = document.get("elements", [])
            if any(not isinstance(element, ObjectId) for element in elements):
                invalid_lists += 1
                logger.warning("Invalid element payload in string_data_list %s", document["_id"])
                continue
            if not elements:
                continue
            existing_ids = {
                item["_id"]
                async for item in db["string_data"].find({"_id": {"$in": elements}}, {"_id": 1})
            }
            missing = [element for element in elements if element not in existing_ids]
            if missing:
                missing_refs += 1
                logger.warning("Missing %s StringData refs in list %s", len(missing), document["_id"])
        logger.info("Audit complete: invalid lists=%s, lists with missing refs=%s", invalid_lists, missing_refs)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
