import logging
from pathlib import Path
from pymongo import MongoClient
from simstack.util.database_information import DatabaseInformation
from simstack.util.toml_reader import TomlReader

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def migrate_array_storage():
    """
    Migration script for ArrayStorage model changes:
    1. Rename 'name' field to 'field_name'.
    2. Add 'storage_mode' field with default value 'auto'.
    """
    try:
        # Load database information from simstack.toml
        # TomlReader(config_path, config_file)
        toml_reader = TomlReader(Path("."), Path("simstack.toml"))
        db_info = DatabaseInformation.from_config(toml_reader.config)
        
        if not db_info.connection_string:
            logger.error("No connection string found in configuration.")
            return

        client = MongoClient(db_info.connection_string)
        db = client[db_info.db_name]
        collection = db.array_storage

        logger.info(f"Starting migration for ArrayStorage in database '{db_info.db_name}'...")

        # 1. Rename 'name' to 'field_name' where 'name' exists and 'field_name' does not
        rename_result = collection.update_many(
            {"name": {"$exists": True}, "field_name": {"$exists": False}},
            {"$rename": {"name": "field_name"}}
        )
        logger.info(f"Renamed 'name' to 'field_name' in {rename_result.modified_count} documents.")

        # 2. Set default storage_mode to 'auto' for documents that don't have it
        mode_result = collection.update_many(
            {"storage_mode": {"$exists": False}},
            {"$set": {"storage_mode": "in-memory"}}
        )
        logger.info(f"Set 'storage_mode' to 'in-memory' in {mode_result.modified_count} documents.")

        logger.info("Migration for ArrayStorage completed successfully.")

    except Exception as e:
        logger.error(f"Migration failed: {e}")
    finally:
        if 'client' in locals():
            client.close()

if __name__ == "__main__":
    migrate_array_storage()
