import asyncio
from pathlib import Path
import re

from simstack.util.project_root_finder import find_project_root
from simstack.util.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation
from simstack.util.db import Database
from simstack.models.log_entry_model import LogEntry

async def clean_logs():
    # Load configuration from simstack.toml in the project root
    project_root = find_project_root()
    toml_reader = TomlReader(project_root, Path("../../simstack.toml"))

    # Initialize database information from config
    db_info = DatabaseInformation.from_config(toml_reader.config)

    # Initialize database connection
    db = Database.from_db_info(db_info)

    needle = "Checking for updates"

    print(f"Connected to database: {db_info.db_name}")
    print(f'Deleting logs where message contains: "{needle}"')

    collection = db.engine.get_collection(LogEntry)
    result = await collection.delete_many(
        {"message": {"$regex": re.escape(needle)}}
    )

    print(f"Deleted {result.deleted_count} log entries.")

if __name__ == "__main__":
    asyncio.run(clean_logs())
