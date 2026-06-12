"""Rewrite embedded FileList payloads to ObjectId-based elements.

Why this exists:
- release-candidate style branches switch embedded `FileList` payloads from
  `file_stacks` to `elements: List[ObjectId]`

What it changes:
- `file_list_io.file_list`
- `node_registry.info_files`

The script persists embedded file stack dictionaries into `file_stack`,
rewrites the parent embedded object, and removes `file_stacks`.
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger, upsert_embedded_documents


async def migrate_field(collection, field_name: str, db) -> int:
    migrated = 0
    async for document in collection.find({f"{field_name}.file_stacks": {"$exists": True}}):
        embedded = dict(document[field_name])
        embedded["elements"] = await upsert_embedded_documents(db, "file_stack", embedded.get("file_stacks", []))
        embedded.pop("file_stacks", None)
        await collection.update_one({"_id": document["_id"]}, {"$set": {field_name: embedded}})
        migrated += 1
    return migrated


async def migrate():
    logger = get_logger("migrate_embedded_filelists_to_refs")
    client, db = await connect_database()
    try:
        file_list_io_count = await migrate_field(db["file_list_io"], "file_list", db)
        node_registry_count = await migrate_field(db["node_registry"], "info_files", db)
        logger.info("Migrated %s file_list_io.file_list payloads", file_list_io_count)
        logger.info("Migrated %s node_registry.info_files payloads", node_registry_count)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
