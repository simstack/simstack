"""Rewrite NodeRegistry project references and flatten Project tags.

Why this exists:
- file-transfer branches change `NodeRegistry.project` from project ObjectId to
  project-name string
- the same branches flatten `Project.tag_ids` into `tags: List[str]`

What it changes:
- `node_registry.project`
- `projects.tag_ids` -> `tags`
- optionally renames `projects` -> `project` when the branch-style target
  collection does not exist yet
"""

from __future__ import annotations

import asyncio

from bson import ObjectId

from _mongo_migration_utils import (
    connect_database,
    get_logger,
    list_collection_names,
    rename_collection_if_present,
)


async def migrate():
    logger = get_logger("migrate_project_strings_and_tags")
    client, db = await connect_database()
    try:
        project_names = {
            document["_id"]: document.get("field_name", "default")
            async for document in db["projects"].find({}, {"field_name": 1})
        }
        tag_names = {
            document["_id"]: document.get("name")
            async for document in db["tags"].find({}, {"name": 1})
        }

        node_registry_updates = 0
        async for document in db["node_registry"].find({"project": {"$type": "objectId"}}):
            project_name = project_names.get(document["project"], "default")
            await db["node_registry"].update_one(
                {"_id": document["_id"]},
                {"$set": {"project": project_name}},
            )
            node_registry_updates += 1
        logger.info("Rewrote %s node_registry.project values", node_registry_updates)

        project_updates = 0
        async for document in db["projects"].find({"tag_ids": {"$exists": True}}):
            tags = [tag_names[tag_id] for tag_id in document.get("tag_ids", []) if isinstance(tag_id, ObjectId) and tag_id in tag_names]
            await db["projects"].update_one(
                {"_id": document["_id"]},
                {"$set": {"tags": tags}, "$unset": {"tag_ids": ""}},
            )
            project_updates += 1
        logger.info("Flattened tags on %s project documents", project_updates)

        collections = await list_collection_names(db)
        if "projects" in collections and "project" not in collections:
            await rename_collection_if_present(db, "projects", "project", logger)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
