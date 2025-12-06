from logging import Logger
from pathlib import Path
from typing import List, Dict, Any

from simstack.models import resource_definition
from simstack.models.parameters import Resource
from simstack.models.resource_definition import ResourceDefinition, GitRepo
from simstack.util.init_data_source import initialize_resource_from_db, initialize_paths_from_db
from simstack.util.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation


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

    :ivar secret_key: The secret key read from the configuration file (if specified).
    :type secret_key: str
    :ivar docker: Whether Docker parameters are enabled in the configuration.
    :type docker: bool
    :ivar external_source_dir: The path to an external source directory, if specified.
    :type external_source_dir: pathlib.Path | None
    """

    def __init__(self, db_info: DatabaseInformation, resource_definition: ResourceDefinition, **kwargs):
        DatabaseInformation.__init__(self, *db_info.get_information())
        # Create a new resource definition from the existing one
        rd_dict = resource_definition.copy()
        ResourceDefinition.__init__(self, **rd_dict)
        self.__dict__ = {**self.__dict__, **kwargs}

    @classmethod
    async def create(cls, resource_str, db: "Database", toml_reader: TomlReader, **kwargs):
        """
        Asynchronous class method for creating an instance using either keyword arguments
        or configuration from external sources (e.g., a database or a TOML file). This method
        initializes the necessary parameters to create the instance, ensuring required keys
        are provided. It supports both cases where initialization details are provided
        directly or fetched from an external configuration source.

        As side effects, it initializes the allowed resources, the route_table, and the path_manager.

        Attributes:
            required_keys: List of strings representing the mandatory keys that must be initialized.
            init_done: Boolean indicating whether all required keys have been initialized.
            init_dict: Dictionary storing the initialized key-value pairs.
            resource_definition: Object that contains resource details fetched from the
                                 database or a configuration file.

        Args:
            resource_str (str): A string key representing the resource for initialization.
            db (Database): Database instance used to fetch initialization parameters when required.
            toml_reader (TomlReader): Reader instance to access configuration values from a TOML file.
            **kwargs: Additional keyword arguments for resource initialization that will
                      override default values from the external sources.

        Returns:
            cls: Returns an initialized instance of the class.

        Raises:
            ValueError: When no valid initialization data is found from either the keyword arguments,
                        configuration file, or database.
        """

        import logging
        logger = logging.getLogger(__name__)  # do this here because the calling function sets the logger up

        required_keys = ["resource", "python_path", "ssh_key", "git_list", "allowed_resources",
                         "workdir", "environment_start"]
        init_done = False
        config = {}
        for key in required_keys:
            if key in kwargs:
                config[key] = kwargs.get(key)
                logger.info(f"Init from kwargs: {key}: {kwargs.get(key)}")
            else:
                init_done = False

        resource_definition = None
        if not init_done:
            if not toml_reader:
                toml_reader = TomlReader()
            use_db_for_init = toml_reader.get("parameters.common.use_db", False)
            logger.info(f"toml-file read, use_db_for_init: {use_db_for_init}")
            if use_db_for_init:  # get all data from the simstack.toml file
                # this will give python_path, ssh_key, and initialize allowed resources
                resource_definition = initialize_resource_from_db(resource_str, db)
                initialize_paths_from_db(db)
            else:
                resource_definition = toml_reader.get_resource_definition(resource_str)
                toml_reader.initialize_path_manager()

        if resource_definition is None:
            raise ValueError("No valid resource definition found.")

        # override the values in resource definition with those from the keyword arguments
        for key in resource_definition.keys():
            if key in config:
                resource_definition[key] = config[key]
                del config[key]
                del required_keys[required_keys.index(key)]

        return cls(db, resource_definition, **config)

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
