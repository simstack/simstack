"""Extract embedded ArrayStorage entries into the array_storage collection.

Why this exists:
- branch `feature-object-list-mixin`
- `ArrayList.array_list` becomes `ArrayList.elements`
- `elements` now stores ObjectIds instead of embedded payloads

What it changes:
- persists embedded `array_list[]` items into `array_storage`
- rewrites each `array_list` document to `elements: [ObjectId, ...]`
- removes the old `array_list` key
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger, upsert_embedded_documents


async def migrate():
    logger = get_logger("migrate_array_lists")
    client, db = await connect_database()
    try:
        collection = db["array_list"]
        migrated = 0
        async for document in collection.find({"array_list": {"$exists": True}}):
            element_ids = await upsert_embedded_documents(db, "array_storage", document.get("array_list", []))
            await collection.update_one(
                {"_id": document["_id"]},
                {"$set": {"elements": element_ids}, "$unset": {"array_list": ""}},
            )
            migrated += 1
        logger.info("Migrated %s array_list documents", migrated)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
