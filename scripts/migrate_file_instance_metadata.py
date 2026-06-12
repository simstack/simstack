"""Backfill newer FileInstance metadata fields on embedded file instances.

Why this exists:
- branch `feature-token-based-filestack-transfer`
- newer FileInstance payloads expect stable ids and lifecycle metadata
- branch validators already tolerate missing values, so this is mainly a
  consistency/backfill migration rather than a hard compatibility rewrite

What it changes:
- embedded `files.file_instances[]`
- fills `id`, `location_type`, `status`, `is_authoritative`, `is_cached`
- leaves richer metadata such as checksums untouched if it is still unknown
"""

from __future__ import annotations

import asyncio
import uuid

from _mongo_migration_utils import connect_database, get_logger


def normalize_instance(instance):
    changed = False
    if not instance.get("id"):
        instance["id"] = str(uuid.uuid4())
        changed = True
    if not instance.get("location_type"):
        instance["location_type"] = "local_path"
        changed = True
    if not instance.get("status"):
        instance["status"] = "available"
        changed = True
    if instance.get("is_authoritative") is None:
        instance["is_authoritative"] = True
        changed = True
    if instance.get("is_cached") is None:
        instance["is_cached"] = False
        changed = True
    return changed


async def migrate():
    logger = get_logger("migrate_file_instance_metadata")
    client, db = await connect_database()
    try:
        collection = db["file_stack"]
        migrated = 0
        async for document in collection.find({"file_instances": {"$exists": True}}):
            changed = False
            file_instances = [dict(item) for item in document.get("file_instances", [])]
            for item in file_instances:
                changed = normalize_instance(item) or changed
            if not changed:
                continue
            await collection.update_one(
                {"_id": document["_id"]},
                {"$set": {"file_instances": file_instances}},
            )
            migrated += 1
        logger.info("Backfilled %s file_stack documents", migrated)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
