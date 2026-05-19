import asyncio
import socket
from pathlib import Path
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.db import Database
from simstack.util.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation

async def main():
    project_root = Path.cwd()
    toml_reader = TomlReader(project_root)
    
    # Get database information from simstack.toml
    db_info = DatabaseInformation.from_config(toml_reader.config)
    
    # Connect to the database
    db = Database.from_db_info(db_info)
    
    hostname = socket.gethostname()
    workdir_self = toml_reader.get("parameters.general.workdir_self")
    if not workdir_self:
         workdir_self = str(project_root / "simstack_workdir")

    print(f"Initializing resources in database: {db_info.db_name}")
    print(f"Hostname: {hostname}")
    print(f"Workdir self: {workdir_self}")

    # Create ResourceDefinition for 'self'
    resource_self = ResourceDefinition(
        resource_str="self",
        workdir=str(workdir_self),
        hostname=hostname,
        python_paths=[],
        environment_start=None,
        ssh_key=None,
        routes=[],
        is_default=False
    )

    # Create ResourceDefinition for 'local'
    resource_local = ResourceDefinition(
        resource_str="local",
        workdir=str(workdir_self),
        hostname=hostname,
        python_paths=[],
        environment_start=None,
        ssh_key=None,
        routes=[],
        is_default=True
    )

    # Upsert records into the database
    await db.upsert(resource_self)
    await db.upsert(resource_local)
    
    print("Successfully wrote 'self' and 'local' ResourceDefinition records to the database.")
    db.close()

if __name__ == "__main__":
    asyncio.run(main())
