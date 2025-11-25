import os
import sys
import tomllib
from pathlib import Path
from typing import List, Dict

from simstack.util.project_root_finder import find_project_root


class ConfigReader:
    """
    Handles the loading and processing of a TOML configuration file,
    to retrieve relevant settings for the workflow environment.
    Provides critical parameters such as database name and connection string are provided, and
    allows resource-based configurations for flexibility.

    :ivar config: The dictionary representation of the parsed configuration file.
    :type config: dict
    :ivar _resource: Specifies the resource type or scope used to determine configurations.
    :type _resource: str
    :ivar _db_name: The name of the database specified in the configuration.
    :type _db_name: str
    :ivar _connection_string: The connection string for the database specified in the configuration.
    :type _connection_string: str
    :ivar _workdir: The working directory based on the resource settings from the configuration.
    :type _workdir: Path | None
    :ivar _python_path: The Python executable path defined in the resource settings from the configuration.
    :type _python_path: str | None
    :ivar _environment_start: Environment-specific start command extracted from the resource settings.
    :type _environment_start: str | None
    """

    def __init__(self, **kwargs):
        config_path = (
            kwargs["config_path"] if "config_path" in kwargs else find_project_root()
        )
        config_file = (
            kwargs["config_file"] if "config_file" in kwargs else "simstack.toml"
        )
        try:
            toml_file = os.path.join(config_path, config_file)
            if os.path.exists(toml_file):
                with open(toml_file, "rb") as f:
                    self.config = tomllib.load(f)
            elif kwargs.get("is_test", False):
                self.config = {
                    "paths": {
                        "tests": {"path": "tests", "drops": "", "use_pickle": False}
                    }
                }
            else:
                print(f"Config file {toml_file} does not exist. Aborting.")
                sys.exit(-1)

        except tomllib.TOMLDecodeError:
            print("There was an error decoding the TOML file.")
            sys.exit(-1)

        self._resource = kwargs.get("resource", "local")
        self._is_test = kwargs.get("is_test", False)
        self._secret_key = None
        # parameter overrides config file
        self._db_name = kwargs.get("db_name", None)

        # for tests we can use an in_memory db
        if self._db_name is None:
            # the package simstack.toml has no db_name and connections string
            self._db_name = (
                self.config.get("parameters", {})
                .get("common", {})
                .get("database", "NONE")
            )
            if not self._is_test and self._db_name == "NONE":
                print("You must specify a database name in the config file")
                sys.exit(-1)

        self._connection_string = kwargs.get("connection_string", None)
        if self._connection_string is None:
            self._connection_string = (
                self.config.get("parameters", {})
                .get("common", {})
                .get("connection_string", "NONE")
            )
        if not self._is_test and self._connection_string == "NONE":
            print("You must specify a connection string in the config file")
            sys.exit(-1)

        self._docker = False
        self._workdir = None
        self._external_workdir = None

        self._python_path = None
        self._external_source_dir = None
        self._environment_start = None
        self._routes = []
        self._git = []

    def secondary_init(self, workdir):
        """
        Called to perform secondary initialization after the context has been set.

        This method is responsible for configuring certain parameters such as the
        working directory (`workdir`), Python path (`python_path`), and the environment
        start command (`environment_start`). It fetches these setups from the provided
        configuration. If critical settings such as the `workdir` are not provided,
        an error is logged, and a `ValueError` is raised. Logging is used throughout
        to provide information about the initialization process.

        :raises ValueError: If the `workdir` configuration for the resource is not specified.
        """

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
