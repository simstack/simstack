"""Rename tuple-dataset selection collection.

Why this exists:
- `feature_new_datasets` moves persisted selections from `DataSetSelection`
  to `DataSetTupleSelection`

What it changes:
- renames collection `data_set_selection` -> `data_set_tuple_selection`

This is intentionally small because the document body is already compatible; the
main issue is the collection/model name split.
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger, rename_collection_if_present


async def migrate():
    logger = get_logger("migrate_dataset_selections")
    client, db = await connect_database()
    try:
        await rename_collection_if_present(
            db,
            "data_set_selection",
            "data_set_tuple_selection",
            logger,
        )
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
