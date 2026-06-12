"""Shared helpers for concise MongoDB migration scripts in the simstack repo.

The goal here is deliberately small:
- load `simstack.toml`
- connect to the configured Mongo database
- expose a couple of tiny helpers that keep the migration scripts readable

Each migration script is still expected to keep its data-rewrite logic inline so
the operational behavior stays obvious during review.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from simstack.util.project_root_finder import find_project_root
from simstack.util.toml_reader import TomlReader


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    return logging.getLogger(name)


def load_db_config() -> tuple[Path, str, str]:
    project_root = find_project_root()
    reader = TomlReader(config_path=project_root, config_file=Path("simstack.toml"))
    connection_string = reader.get("parameters.db.connection_string")
    database_name = reader.get("parameters.db.database")
    if not connection_string or not database_name:
        raise RuntimeError("Database configuration not found in simstack.toml")
    return project_root, connection_string, database_name


async def connect_database():
    _, connection_string, database_name = load_db_config()
    client = AsyncIOMotorClient(connection_string)
    return client, client[database_name]


async def list_collection_names(db) -> set[str]:
    return set(await db.list_collection_names())


async def rename_collection_if_present(db, old_name: str, new_name: str, logger: logging.Logger) -> bool:
    collections = await list_collection_names(db)
    if old_name not in collections:
        logger.info("Skipping collection rename %s -> %s; source is absent", old_name, new_name)
        return False
    if new_name in collections:
        logger.info("Skipping collection rename %s -> %s; target already exists", old_name, new_name)
        return False
    await db[old_name].rename(new_name)
    logger.info("Renamed collection %s -> %s", old_name, new_name)
    return True


async def upsert_embedded_documents(db, collection_name: str, raw_items: list) -> list[ObjectId]:
    """Persist embedded dictionaries into a top-level collection and return ids.

    Existing ObjectIds pass through unchanged. Embedded dictionaries are inserted
    or replaced by `_id`.
    """

    collection = db[collection_name]
    object_ids: list[ObjectId] = []
    for raw_item in raw_items or []:
        if isinstance(raw_item, ObjectId):
            object_ids.append(raw_item)
            continue
        if not isinstance(raw_item, dict):
            continue
        document = dict(raw_item)
        document_id = document.get("_id") or ObjectId()
        document["_id"] = document_id
        await collection.replace_one({"_id": document_id}, document, upsert=True)
        object_ids.append(document_id)
    return object_ids

