from pathlib import Path
from typing import List, Dict

from simstack.models.resource_definition import ResourceDefinition
from simstack.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation
from simstack.util.db import Database


class ConfigReader(DatabaseInformation):
    """
    Handles the loading and processing of a TOML configuration file,
    to retrieve relevant settings for the workflow environment.
    Provides critical parameters such as database name and connection string are provided, and
    allows resource-based configurations for flexibility.

    :ivar _resource: Specifies the resource type or scope used to determine configurations.
    :type _resource: str
    :ivar _workdir: The working directory.
    :type _workdir: Path | None
    :ivar _python_path: The Python executable path defined in the resource settings from the configuration.
    :type _python_path: str | None
    :ivar _environment_start: Environment-specific start command extracted from the resource settings.
    :type _environment_start: str | None
    """

    def __init__(self, db: Database, toml_reader: TomlReader, **kwargs):
        super().__init__(*db.get_information())
        
        # the resource must be in the kwargs 
        self._resource: str | None = kwargs.get("resource",None)

        resource_records = await db.find_all(ResourceDefinition)
        if resource_records is not None:
            self._allowed_resources = [r.name for r in resource_records]
            if self._allowed_resources is None or self._resource not in self._allowed_resources:

        self._is_test = kwargs.get("is_test", False)
        self._secret_key = None
        # parameter overrides config file
      
        self._docker = False
        self._workdir = None
        self._external_workdir = None

        self._python_path = None
        self._external_source_dir = None
        self._environment_start = None
        self._routes = []
        self._git = []
        self._resources = []
        self._allowed_resources = []

   
        import logging

        logger = logging.getLogger("ConfigReader")

        logger.info(
            f"Initializing ConfigReader with resource: {self._resource} on database {self._db_name}"
        )

        self._docker = (
            self.config.get("parameters", {})
            .get(self._resource, {})
            .get("docker", False)
        )
        logger.info(f"docker: {self._docker}")
        if workdir is None:
            workdir = (
                self.config.get("parameters", {})
                .get(self._resource, {})
                .get("workdir", "NONE")
            )
        if workdir == "NONE":
            logger.error(
                f"You must specify a working directory for resource: {self._resource} in the config file"
            )
            raise ValueError(
                f"You must specify a working directory for resource: {self._resource} in the config file"
            )
        self._workdir = Path(workdir)
        logger.info(f"workdir: {self._workdir}")

        if self._docker:
            self._external_workdir = workdir
            self._workdir = Path("/home/appuser/simstack")
            logger.info(f"external_workdir: {self._external_workdir}")

        self._external_source_dir = Path(
            self.config.get("parameters", {})
            .get(self._resource, {})
            .get("source_dir", "NONE")
        )
        logger.info(f"source directory: {self._external_source_dir}")
        if self.docker and self._external_source_dir == Path("NONE"):
            logger.error(
                f"You must specify an external source directory for resource: {self._resource} in the config file"
            )
            raise ValueError(
                f"You must specify an external source directory for resource: {self._resource} in the config file"
            )

        self._python_path = (
            self.config.get("parameters", {})
            .get(self._resource, {})
            .get("python_path", "NONE")
        )
        if self._python_path == "NONE":
            logger.error("PYTHON PATH IS MISSING")
        logger.info(f"python_path: {self._python_path}")

        self._environment_start = (
            self.config.get("parameters", {})
            .get(self._resource, {})
            .get("environment_start", "")
        )
        logger.info(f"environment_start: {self._environment_start}")

        self._resources = (
            self.config.get("parameters", {}).get("common", {}).get("resources", [])
        )
        logger.info(f"Initialized resources to: {self._resources}")

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

        self._secret_key = self.config.get("server", {}).get("secret_key", "")
        self._git = (
            self.config.get("parameters", {}).get(self._resource, {}).get("git", [])
        )

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

    @property
    def allowed_resources(self) -> List[str]:
        return self._allowed_resources

    @property
    def secret_key(self) -> str:
        return self._secret_key

    @property
    def docker(self) -> bool:
        return self._docker

    @property
    def environment_start(self) -> str:
        return self._environment_start

    @property
    def python_path(self) -> str:
        return self._python_path

    @property
    def workdir(self) -> Path:
        return self._workdir

    @property
    def external_workdir(self) -> Path:
        return self._external_workdir

    @property
    def external_source_dir(self) -> Path:
        return self._external_source_dir

    @property
    def git(self) -> List[Dict]:
        return self._git

    @property
    def connection_string(self) -> str:
        return self._connection_string

    @property
    def database_name(self) -> str:
        return self._db_name

    @property
    def resource(self) -> str:
        return self._resource

    @resource.setter
    def resource(self, value: str):
        self._resource = value

    @property
    def paths(self) -> Dict:
        """
        Get the path configuration from the TOML file.

        Returns:
            Dictionary containing path configurations or an empty dict if not found
        """
        return self.config.get("paths", {})
