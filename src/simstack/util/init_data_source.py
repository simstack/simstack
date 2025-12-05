from simstack.core.resources import allowed_resources
from simstack.core.route_table import route_table
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.db import Database
from simstack.util.toml_reader import TomlReader

import logging
logger = logging.getLogger(__name__)

async def initialize_resource_from_db(resource_str: str, db: Database) -> ResourceDefinition:
    resource_records = await db.find_all(ResourceDefinition)
    if resource_records is not None:
        # this is the sign that we can initialize from the database
        allowed_resources_list = [r.name for r in resource_records]
        logger.info(f"Initializing ConfigReader from database, allowed resources: {allowed_resources_list}")
        if resource_str not in allowed_resources_list:
            raise ValueError(f"Resource {resource_str} not found in the list of allowed resources")

        # Find the resource definition matching the resource name
        resource_definition = next((r for r in resource_records if r.name == resource_str), None)
        if resource_definition is None:
            raise ValueError(f"Resource definition for {resource_str} not found")
        logger.info(f"ConfigReader resources: {allowed_resources_list}")
        allowed_resources.set_resources(allowed_resources_list)

        # build the route definition table
        for resource_record in resource_records:
            route_table.add_route(resource_record.name, resource_record.routes)
        return resource_definition
    else:
        raise ValueError("No resources found in the database")

def initialize_paths_from_db(db:Database):
    logger.warning("Initializing paths from database is not yet implemented")
    pass
