"""Migrate dataset collection names and tuple registry references.

Why this exists:
- tuple-style `DataSet` is split into `DataSetTuple`
- some branch families also rename `data_set_dict` -> `data_set`
- `NodeRegistry.input_tables` / `result_tables` still point at `DataSet`

What it changes:
- `data_set` -> `data_set_tuple` when needed
- `data_set_dict` -> `data_set` when needed
- rewrites `node_registry.input_tables[]` and `result_tables[]` from `DataSet`
  to `DataSetTuple`
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger, rename_collection_if_present


async def migrate():
    logger = get_logger("migrate_dataset_collections")
    client, db = await connect_database()
    try:
        await rename_collection_if_present(db, "data_set", "data_set_tuple", logger)
        await rename_collection_if_present(db, "data_set_dict", "data_set", logger)

        input_result = await db["node_registry"].update_many(
            {"input_tables": "DataSet"},
            {"$set": {"input_tables.$[entry]": "DataSetTuple"}},
            array_filters=[{"entry": "DataSet"}],
        )
        output_result = await db["node_registry"].update_many(
            {"result_tables": "DataSet"},
            {"$set": {"result_tables.$[entry]": "DataSetTuple"}},
            array_filters=[{"entry": "DataSet"}],
        )
        logger.info("Updated %s node_registry input_tables entries", input_result.modified_count)
        logger.info("Updated %s node_registry result_tables entries", output_result.modified_count)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
