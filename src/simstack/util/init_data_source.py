from pathlib import Path

from simstack.core.resources import allowed_resources
from simstack.models.resource_definition import ResourceDefinition
from simstack.persistence import Repository

import logging

logger = logging.getLogger("init_resource")


async def load_resource_definition(
    resource_str: str,
    repository: Repository,
    workdir_self: Path,
) -> ResourceDefinition:
    resource_records = await repository.find(ResourceDefinition)
    if not resource_records:
        raise ValueError("No resources found in the repository")

    allowed_resources_list = [record.resource_str for record in resource_records]
    if "self" not in allowed_resources_list:
        allowed_resources_list.append("self")
    if resource_str not in allowed_resources_list:
        raise ValueError(
            f"Resource {resource_str} not found in the list of allowed resources"
        )

    if resource_str == "self":
        resource_definition = ResourceDefinition(
            resource_str=resource_str,
            hostname="localhost",
            workdir=workdir_self,
            routes=[],
        )
    else:
        resource_definition = next(
            (
                record
                for record in resource_records
                if record.resource_str == resource_str
            ),
            None,
        )
        if resource_definition is None:
            raise ValueError(f"Resource definition for {resource_str} not found")

    logger.info("ConfigReader resources: %s", allowed_resources_list)
    allowed_resources.set_resources(allowed_resources_list)
    return resource_definition
