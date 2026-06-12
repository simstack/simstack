"""Rename DataSetMetadataTemplate.dataset_type to field_name.

Why this exists:
- branches `cursor/ci-mongo-setup-e833f` and `feature-file-transfer`
- target model looks up `DataSetMetadataTemplate.field_name`
- old documents only store `dataset_type`

What it changes:
- `data_set_metadata_template.dataset_type` -> `field_name`
- recreates a unique index on `field_name`

This script is idempotent for already-migrated documents.
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger


async def migrate():
    logger = get_logger("migrate_dataset_metadata_field_name")
    client, db = await connect_database()
    try:
        collection = db["data_set_metadata_template"]
        result = await collection.update_many(
            {"dataset_type": {"$exists": True}},
            [
                {"$set": {"field_name": "$dataset_type"}},
                {"$unset": "dataset_type"},
            ],
        )
        logger.info("Rewrote %s template documents", result.modified_count)

        indexes = await collection.index_information()
        if "dataset_type_1" in indexes:
            await collection.drop_index("dataset_type_1")
            logger.info("Dropped legacy index dataset_type_1")
        await collection.create_index("field_name", unique=True)
        logger.info("Ensured unique index on field_name")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
