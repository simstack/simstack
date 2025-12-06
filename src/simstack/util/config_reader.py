from logging import Logger
from pathlib import Path
from typing import List, Dict, Any, TYPE_CHECKING

from mypy.dmypy.client import restart_server

from simstack.core.resources import allowed_resources
from simstack.models.parameters import Resource
from simstack.models.resource_definition import ResourceDefinition, GitRepo
from simstack.util.init_data_source import initialize_resource_from_db, initialize_paths_from_db
from simstack.util.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation

if TYPE_CHECKING:
    from simstack.util.db import Database

class ConfigReader(DatabaseInformation):
    """
    Represents a configuration reader that integrates with a database and a resource
    definition system. The class is designed to read configurations from multiple sources,
    including databases and TOML files, and exposes interfaces to obtain detailed resource
    and routing information.

    The primary purpose of this class is to manage application configurations, validate
    resource definitions, and provide utility methods for retrieving specific routing
    and resource data.

    inherits from DatabaseInformation and the attributes of ResourceDefinition



    """

    def __init__(self, db_info: DatabaseInformation, resource_definition: ResourceDefinition, **kwargs):
        DatabaseInformation.__init__(self, *db_info.get_information())
        self._git_list = kwargs.get("_git_list", [])
        self._resource_str = resource_definition.resource_str
        self.__dict__ = {**self.__dict__, **kwargs}
        self.__dict__.update(resource_definition.__dict__)


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
        git_list = []
        if not init_done:
            if not toml_reader:
                toml_reader = TomlReader()
            use_db_for_init = toml_reader.use_db()
            logger.info(f"toml-file read, use_db_for_init: {use_db_for_init}")
            if use_db_for_init:  # get all data from the simstack.toml file
                # this will give python_path, ssh_key, and initialize allowed resources
                resource_definition = await initialize_resource_from_db(resource_str, db)
                await initialize_paths_from_db(db)
            else:
                allowed_resources_list = toml_reader.get_allowed_resources()
                allowed_resources.set_resources(allowed_resources_list)
                resource_definition = toml_reader.get_resource_definition(resource_str)
                git_list = toml_reader.get_git_list()
                toml_reader.initialize_path_manager()
                toml_reader.build_routes()

        if resource_definition is None:
            raise ValueError("No valid resource definition found.")

        # override the values in resource definition with those from the keyword arguments
        for key in resource_definition.model_fields.keys():
            if key in config:
                resource_definition.__setattr__(key, config[key])
                del config[key]
                del required_keys[required_keys.index(key)]

        if "git_list" in config:
            config["_git_list"] = config["git_list"]
            del config["git_list"]
        else:
            config["_git_list"] = git_list

        return cls(db, resource_definition, **config)

    @property
    def git_list(self) -> List[GitRepo]:
        return self._git_list

    @property
    def resource(self) -> Resource:
        return Resource(value=self._resource_str)

    @resource.setter
    def resource(self, value: str):
        raise ValueError("ConfigReader: Resource cannot be set directly")
