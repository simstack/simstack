"""Extract FileListModel.file_stacks into top-level file_stack references.

Why this exists:
- multiple branch families move `FileListModel.file_stacks`
  from embedded `FileStack` payloads to `elements: List[ObjectId]`

What it changes:
- persists embedded file stacks into `file_stack`
- rewrites each `file_list_model` document to `elements`
- removes `file_stacks`
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger, upsert_embedded_documents


async def migrate():
    logger = get_logger("migrate_filelist_models")
    client, db = await connect_database()
    try:
        collection = db["file_list_model"]
        migrated = 0
        async for document in collection.find({"file_stacks": {"$exists": True}}):
            element_ids = await upsert_embedded_documents(db, "file_stack", document.get("file_stacks", []))
            await collection.update_one(
                {"_id": document["_id"]},
                {"$set": {"elements": element_ids}, "$unset": {"file_stacks": ""}},
            )
            migrated += 1
        logger.info("Migrated %s file_list_model documents", migrated)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
