import asyncio
import socket
import argparse
import sys
from pathlib import Path
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.db import Database
from simstack.util.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation

async def main():
    parser = argparse.ArgumentParser(description="Initialize resources in the database.")
    parser.add_argument("config_path", type=str, help="Path to the simstack.toml configuration file.")
    args = parser.parse_args()

    config_file = Path(args.config_path)
    if not config_file.exists():
        print(f"Error: Config file {config_file} does not exist.")
        return

    toml_reader = TomlReader(config_file.parent, config_file.name)
    
    # Get database information from simstack.toml
    db_info = DatabaseInformation.from_config(toml_reader.config)
    
    # Connect to the database
    db = Database.from_db_info(db_info)
    
    hostname = socket.gethostname()
    project_root = config_file.parent.parent.parent.parent # Assuming src/simstack/util/simstack.toml
    # Actually it is better to find project root or use current dir as fallback
    workdir_self = toml_reader.get("parameters.general.workdir_self")
    if not workdir_self:
         workdir_self = str(Path.cwd() / "simstack_workdir")

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
    try:
        # Check connection
        print(f"Connecting to database {db_info.db_name} at {db_info.connection_string}...")
        # Use the motor client's command method via engine's database holder
        db_raw = db.client[db_info.db_name]
        await db_raw.command("ping")
        print("Database connection successful.")
        
        await db.upsert(resource_self)
        await db.upsert(resource_local)
        print("Successfully wrote 'self' and 'local' ResourceDefinition records to the database.")
    except Exception as e:
        print(f"Error connecting to or writing to the database: {e}")
        import traceback
        traceback.print_exc()
        db.close()
        sys.exit(1)
    
    db.close()

if __name__ == "__main__":
    asyncio.run(main())
