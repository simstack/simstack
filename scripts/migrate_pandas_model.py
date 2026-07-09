import logging
from pathlib import Path
from pymongo import MongoClient
from simstack.util.database_information import DatabaseInformation
from simstack.util.toml_reader import TomlReader

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def migrate_pandas_model():
    """
    Migration script for PandasModel changes:
    1. Rename 'name' field to 'field_name'.
    2. Add 'storage_mode' field with default value 'auto'.
    3. Rename 'content' field to 'content_' if it exists.
    """
    try:
        # Load database information from simstack.toml
        toml_reader = TomlReader(Path("."), Path("simstack.toml"))
        db_info = DatabaseInformation.from_config(toml_reader.config)
        
        if not db_info.connection_string:
            logger.error("No connection string found in configuration.")
            return

        client = MongoClient(db_info.connection_string)
        db = client[db_info.db_name]
        collection = db.pandas_model

        logger.info(f"Starting migration for PandasModel in database '{db_info.db_name}'...")

        # 1. Rename 'name' to 'field_name' where 'name' exists and 'field_name' does not
        rename_name_result = collection.update_many(
            {"name": {"$exists": True}, "field_name": {"$exists": False}},
            {"$rename": {"name": "field_name"}}
        )
        logger.info(f"Renamed 'name' to 'field_name' in {rename_name_result.modified_count} documents.")

        # 2. Rename 'content' to 'content_' where 'content' exists and 'content_' does not
        rename_content_result = collection.update_many(
            {"content": {"$exists": True}, "content_": {"$exists": False}},
            {"$rename": {"content": "content_"}}
        )
        logger.info(f"Renamed 'content' to 'content_' in {rename_content_result.modified_count} documents.")

        # 3. Set default storage_mode to 'auto' for documents that don't have it
        mode_result = collection.update_many(
            {"storage_mode": {"$exists": False}},
            {"$set": {"storage_mode": "auto"}}
        )
        logger.info(f"Set 'storage_mode' to 'auto' in {mode_result.modified_count} documents.")

        logger.info("Migration for PandasModel completed successfully.")

    except Exception as e:
        logger.error(f"Migration failed: {e}")
    finally:
        if 'client' in locals():
            client.close()

if __name__ == "__main__":
    migrate_pandas_model()
