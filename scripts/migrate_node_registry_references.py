"""Rewrite NodeRegistry input/output arrays into NamedDataReference payloads.

Why this exists:
- branch `feature-data-models`
- MR `#64`
- old NodeRegistry documents store parallel arrays
- new NodeRegistry documents store embedded objects in `input_references` and
  `results_references`

What it changes:
- zips `input_names` + `input_tables` + `input_ids`
- zips `result_names` + `result_tables` + `result_ids`
- preserves any already-present legacy `result_references`
- removes the old parallel-array fields after the rewrite
"""

from __future__ import annotations

import asyncio

from _mongo_migration_utils import connect_database, get_logger


def build_references(names, mappings, object_ids, default_name: str):
    size = max(len(names), len(mappings), len(object_ids))
    return [
        {
            "variable_name": names[index] if index < len(names) else default_name,
            "variable_mapping": mappings[index] if index < len(mappings) else "unknown",
            "reference": object_ids[index],
        }
        for index in range(size)
        if index < len(object_ids) and object_ids[index]
    ]


async def migrate():
    logger = get_logger("migrate_node_registry_references")
    client, db = await connect_database()
    try:
        collection = db["node_registry"]
        query = {
            "$or": [
                {"input_tables": {"$exists": True}},
                {"input_ids": {"$exists": True}},
                {"input_names": {"$exists": True}},
                {"result_tables": {"$exists": True}},
                {"result_ids": {"$exists": True}},
                {"result_names": {"$exists": True}},
                {"result_references": {"$exists": True}},
            ]
        }

        migrated = 0
        async for document in collection.find(query):
            input_references = build_references(
                document.get("input_names", []),
                document.get("input_tables", []),
                document.get("input_ids", []),
                "variable",
            )
            results_references = document.get("result_references") or build_references(
                document.get("result_names", []),
                document.get("result_tables", []),
                document.get("result_ids", []),
                "result",
            )

            await collection.update_one(
                {"_id": document["_id"]},
                {
                    "$set": {
                        "input_references": input_references,
                        "results_references": results_references,
                    },
                    "$unset": {
                        "input_names": "",
                        "input_tables": "",
                        "input_ids": "",
                        "result_names": "",
                        "result_tables": "",
                        "result_ids": "",
                        "result_references": "",
                    },
                },
            )
            migrated += 1
        logger.info("Migrated %s node_registry documents", migrated)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
