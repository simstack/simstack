"""Rename embedded FileList.file_stacks to embedded FileList.elements.

Why this exists:
- branch variants `feature_new_datasets` and `fix-failed-orca-jobs`
- those variants do not yet move embedded FileList entries to ObjectIds
- they only rename the embedded list field from `file_stacks` to `elements`

What it changes:
- `file_list_io.file_list`
- `node_registry.info_files`

Use this only for those exact branch shapes. If you are migrating directly to
release-candidate style ObjectId references, use `migrate_embedded_filelists_to_refs.py`.
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger


async def migrate_field(collection, field_name: str) -> int:
    migrated = 0
    async for document in collection.find({f"{field_name}.file_stacks": {"$exists": True}}):
        embedded = dict(document[field_name])
        embedded["elements"] = embedded.pop("file_stacks", [])
        await collection.update_one({"_id": document["_id"]}, {"$set": {field_name: embedded}})
        migrated += 1
    return migrated


async def migrate():
    logger = get_logger("migrate_embedded_filelists_to_embedded_elements")
    client, db = await connect_database()
    try:
        file_list_io_count = await migrate_field(db["file_list_io"], "file_list")
        node_registry_count = await migrate_field(db["node_registry"], "info_files")
        logger.info("Migrated %s file_list_io.file_list payloads", file_list_io_count)
        logger.info("Migrated %s node_registry.info_files payloads", node_registry_count)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
