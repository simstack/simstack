from pathlib import Path
from typing import List, Dict

from simstack.core.resources import allowed_resources, Resource
from simstack.models.resource_definition import ResourceDefinition, GitRepo
from simstack.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation
from simstack.util.db import Database


class ConfigReader(DatabaseInformation, ResourceDefinition):
    """
    Represents a configuration reader that integrates with a database and a resource
    definition system. The class is designed to read configurations from multiple sources,
    including databases and TOML files, and exposes interfaces to obtain detailed resource
    and routing information.

    The primary purpose of this class is to manage application configurations, validate
    resource definitions, and provide utility methods for retrieving specific routing
    and resource data.

    inheritance: DatabaseInformation, ResourceDefinition

    :ivar config: The entire configuration loaded, typically from a TOML reader.
    :type config: dict
    :ivar allowed_resources: The list of resources allowed to be accessed, either
        from the database or the configuration file.
    :type allowed_resources: List[str]
    :ivar routes: List of routes defined in the configuration file. Each route must
        include 'source', 'target', and 'host' keys.
    :type routes: List[dict]
    :ivar secret_key: The secret key read from the configuration file (if specified).
    :type secret_key: str
    :ivar docker: Whether Docker parameters are enabled in the configuration.
    :type docker: bool
    :ivar external_source_dir: The path to an external source directory, if specified.
    :type external_source_dir: pathlib.Path | None
    """

    def __init__(self, db: DatabaseInformation, resource_definition: ResourceDefinition,
                 allowed_resources: List[str], git_list: List[GitRepo],
                 toml_reader: TomlReader, **kwargs):
        DatabaseInformation.__init__(self, *db.get_information())
        ResourceDefinition.__init__(self, **resource_definition.dict())

        self._allowed_resources = allowed_resources  # these are strings !
        self._is_test = kwargs.get("is_test", False)

        import logging
        logger = logging.getLogger("ConfigReader")
        logger.info(f"Initializing ConfigReader with resource: {self.resource}")
        logger.info(f"ConfigReader kwargs: {kwargs}")
        logger.info(f"ConfigReader db_info: {self.get_database_information()}")
        logger.info(f"ConfigReader resources: {self.allowed_resources}")

        logger.info(f"ConfigReader is_test: {self._is_test}")

        # unused and legacy keys
        self._external_workdir = None
        self._external_source_dir = None
        self._secret_key = None
        self._docker = False  # no longer supported for now
        self._routes = []
        self._git = []
        self._resources = []
        self._allowed_resources = []

        if toml_reader is not None:
            self.config = toml_reader.config
            self._docker = toml_reader.get("parameters.common.docker", False)
            logger.info(f"docker: {self._docker}")

            external_source_dir = toml_reader.get("parameters.common.source_dir", None)
            if external_source_dir is not None:
                self._external_source_dir = Path(external_source_dir)
                logger.info(f"external source directory: {self._external_source_dir}")

            if self.docker and self._external_source_dir == Path("NONE"):
                logger.error(
                    f"You must specify an external source directory for resource: {self._resource} in the config file"
                )
                raise ValueError(
                    f"You must specify an external source directory for resource: {self._resource} in the config file"
                )

            self._routes = self.config.get("routes", [])
            for route in self._routes:
                if not isinstance(route, dict):
                    logger.error(f"Route {route} is not a dictionary.")
                    raise ValueError("Route {route} is not a dictionary.")
                if not ("source" in route and "target" in route and "host" in route):
                    logger.error(
                        f"Route {route} does not contain 'source', 'target', 'host' keys."
                    )
                    raise ValueError(
                        f"Route {route} does not contain 'source', 'target', 'host' keys."
                    )

            self._secret_key = toml_reader.get("server.secret_key", "")

    @classmethod
    async def create(cls, db: Database, toml_reader: TomlReader | None, **kwargs):
        resource = kwargs.get("resource", None)
        if resource is None:
            raise ValueError("Resource must be specified in the kwargs of ConfigReader.create")

        resource_records = await db.find_all(ResourceDefinition)
        if resource_records is not None:
            # this is the sign that we can initialize from the database
            allowed_resources = [r.name for r in resource_records]
            logger.info(f"Intilializing ConfigReader from database, allowed resources: {allowed_resources}")
            if resource not in allowed_resources:
                raise ValueError(f"Resource {resource} not found in the list of allowed resources")

            # Find the resource definition matching the resource name
            resource_definition = next((r for r in resource_records if r.name == resource), None)
            if resource_definition is None:
                raise ValueError(f"Resource definition for {resource} not found")
        elif toml_reader is not None:
            # this is the sign that we can initialize from the toml file
            allowed_resources = toml_reader.get("parameters.common.allowed_resources", None)
            logger.info(f"Intilializing ConfigReader from toml file, allowed resources: {allowed_resources}")

            if allowed_resources is None:
                raise ValueError("Allowed resources not found in the toml file")
            if resource not in allowed_resources:
                raise ValueError(f"Resource {resource} not found in the list of allowed resources")

            resource_definition_dict = toml_reader.get(f"parameters.{resource}", None)
            if resource_definition_dict is None:
                raise ValueError(f"Resource definition for {resource} not found in the toml file")
            resource_definition = ResourceDefinition(**resource_definition_dict)
        else:
            raise ValueError("Either the database must allow initialization or a toml file must be specified")

        git_list = await db.find_all(GitRepo)
        if git_list is None and toml_reader is not None:
            git_list = toml_reader.get("parameters.common.git", [])

        return cls(db, resource_definition, allowed_resources, git_list, toml_reader,
                   **kwargs)

    @property
    def allowed_resources(self) -> List[str]:
        return self._allowed_resources

    @property
    def docker(self) -> bool:
        return self._docker

    @property
    def external_workdir(self) -> Path:
        return self._external_workdir

    @property
    def external_source_dir(self) -> Path:
        return self._external_source_dir

    @property
    def git(self) -> List[GitRepo]:
        return self._git

    @property
    def resource(self) -> Resource:
        return Resource(ResourceDefinition.resource)

    @resource.setter
    def resource(self, value: str):
        raise ValueError("ConfigReader: Resource cannot be set directly")

    def get_route(self, source: str, target: str) -> List[Dict[str, str]]:
        """
        Retrieves the route configuration for a given source and target.

        :param source: The source node.
        :param target: The target node.
        :return: A list of dictionaries representing the route configuration.
        """
        for route in self._routes:
            if route.get("source") == source and route.get("target") == target:
                return route
        return []
