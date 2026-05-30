from pathlib import Path
from simstack.core.resources import allowed_resources
from simstack.core.route_table import route_table
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.db import Database
from simstack.util.toml_reader import TomlReader

import logging
logger = logging.getLogger("init_resource")

async def initialize_resource_from_db(resource_str: str, db: Database, workdir_self: Path) -> ResourceDefinition:
    resource_records = await db.find_all(ResourceDefinition)
    if resource_records is not None:
        # this is the sign that we can initialize from the database
        allowed_resources_list = [r.resource_str for r in resource_records]
        if "self" not in allowed_resources_list:
            allowed_resources_list.append("self")
        if resource_str not in allowed_resources_list:
            raise ValueError(f"Resource {resource_str} not found in the list of allowed resources")

        # Find the resource definition matching the resource name
        if resource_str == "self":
            resource_definition = ResourceDefinition(resource_str=resource_str,
                                                     hostname = "localhost",
                                                     workdir = workdir_self, # self does not need a workdir
                                                     routes=[])
        else: # it makes no sense to initialize self from the database
            resource_definition = next((r for r in resource_records if r.resource_str == resource_str), None)
            if resource_definition is None:
                raise ValueError(f"Resource definition for {resource_str} not found")
        logger.info(f"ConfigReader resources: {allowed_resources_list}")
        allowed_resources.set_resources(allowed_resources_list)

        # build the route definition table
        route_table.clear_routes()
        for resource_record in resource_records:
            route_table.add_route_set(resource_record.resource_str, resource_record.routes)
        return resource_definition
    else:
        raise ValueError("No resources found in the database")

async def initialize_paths_from_db(db:Database):
    pass
