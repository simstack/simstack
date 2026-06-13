import asyncio
import logging
import tomllib as toml
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

from simstack.util.project_root_finder import find_project_root

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

async def migrate():
    # Load connection info from simstack.toml
    project_root = find_project_root()
    config_path = project_root / "simstack.toml"
    if not config_path.exists():
        logger.error("simstack.toml not found")
        return

    with open(config_path, "rb") as f:
        config = toml.load(f)
    db_config = config.get("parameters", {}).get("db", {})
    conn_str = db_config.get("connection_string")
    db_name = db_config.get("database")

    if not conn_str or not db_name:
        logger.error("Connection string or database name not found in simstack.toml")
        return

    logger.info(f"Connecting to {conn_str}, database {db_name}")
    client = AsyncIOMotorClient(conn_str)
    db = client[db_name]
    collection = db["node_registry"]

    cursor = collection.find({
        "$or": [
            {"input_tables": {"$exists": True}},
            {"input_ids": {"$exists": True}},
            {"input_names": {"$exists": True}},
            {"result_tables": {"$exists": True}},
            {"result_ids": {"$exists": True}},
            {"result_names": {"$exists": True}},
            {"result_references": {"$exists": True}}
        ]
    })

    count = 0
    async for doc in cursor:
        doc_id = doc["_id"]
        update = {}

        # Migrate inputs
        input_names = doc.get("input_names", [])
        input_tables = doc.get("input_tables", [])
        input_ids = doc.get("input_ids", [])
        
        input_references = []
        # We need to handle cases where lists might have different lengths or be missing
        max_len = max(len(input_names), len(input_tables), len(input_ids))
        for i in range(max_len):
            ref = {
                "variable_name": input_names[i] if i < len(input_names) else "variable",
                "variable_mapping": input_tables[i] if i < len(input_tables) else "unknown",
                "reference": input_ids[i] if i < len(input_ids) else None
            }
            if ref["reference"]:
                input_references.append(ref)
        
        if input_references or ("input_tables" in doc) or ("input_ids" in doc) or ("input_names" in doc):
            update["input_references"] = input_references

        # Migrate results
        result_names = doc.get("result_names", [])
        result_tables = doc.get("result_tables", [])
        result_ids = doc.get("result_ids", [])
        result_references = doc.get("result_references", [])

        results_references = []
        if result_references:
            results_references = result_references
        else:
            max_len_res = max(len(result_names), len(result_tables), len(result_ids))
            for i in range(max_len_res):
                ref = {
                    "variable_name": result_names[i] if i < len(result_names) else "unknown",
                    "variable_mapping": result_tables[i] if i < len(result_tables) else "unknown",
                    "reference": result_ids[i] if i < len(result_ids) else None
                }
                if ref["reference"]:
                    results_references.append(ref)
        
        if results_references or ("result_tables" in doc) or ("result_ids" in doc) or ("result_names" in doc) or ("result_references" in doc):
            update["results_references"] = results_references

        if update:
            await collection.update_one(
                {"_id": doc_id},
                {
                    "$set": update,
                    "$unset": {
                        "input_names": "",
                        "input_tables": "",
                        "input_ids": "",
                        "result_tables": "",
                        "result_ids": "",
                        "result_names": "",
                        "result_references": ""
                    }
                }
            )
            count += 1
            if count % 100 == 0:
                logger.info(f"Migrated {count} documents")

    logger.info(f"Migration completed. Total migrated: {count}")

if __name__ == "__main__":
    asyncio.run(migrate())
