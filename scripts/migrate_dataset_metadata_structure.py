"""Rewrite dataset metadata.structure from list form to keyed-dict form.

Why this exists:
- tuple/dict dataset branches move metadata structure from:
  `{"section": ["Mol", "QMResult"]}`
  to:
  `{"section": {"0": "Mol", "1": "QMResult"}}`

What it changes:
- `data_set_metadata_template.structure`
- embedded `metadata.structure` inside `data_set` and `data_set_tuple`

The script only rewrites sections that are still stored as lists.
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger, list_collection_names


def normalize_structure(raw_structure):
    if not isinstance(raw_structure, dict):
        return raw_structure
    normalized = {}
    changed = False
    for section_name, section_value in raw_structure.items():
        if isinstance(section_value, list):
            normalized[section_name] = {str(index): item for index, item in enumerate(section_value)}
            changed = True
            continue
        normalized[section_name] = section_value
    return normalized, changed


async def migrate_collection_structures(collection, logger):
    changed_count = 0
    async for document in collection.find({"metadata.structure": {"$type": "object"}}):
        structure = document.get("metadata", {}).get("structure")
        normalized, changed = normalize_structure(structure)
        if not changed:
            continue
        await collection.update_one({"_id": document["_id"]}, {"$set": {"metadata.structure": normalized}})
        changed_count += 1
    logger.info("Rewrote %s embedded metadata structures in %s", changed_count, collection.name)


async def migrate():
    logger = get_logger("migrate_dataset_metadata_structure")
    client, db = await connect_database()
    try:
        template_count = 0
        async for document in db["data_set_metadata_template"].find({"structure": {"$type": "object"}}):
            normalized, changed = normalize_structure(document.get("structure"))
            if not changed:
                continue
            await db["data_set_metadata_template"].update_one(
                {"_id": document["_id"]},
                {"$set": {"structure": normalized}},
            )
            template_count += 1
        logger.info("Rewrote %s metadata template structures", template_count)

        collections = await list_collection_names(db)
        for collection_name in ("data_set", "data_set_tuple"):
            if collection_name in collections:
                await migrate_collection_structures(db[collection_name], logger)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
