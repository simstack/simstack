import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from simstack.util.database_information import DatabaseInformation
from simstack.util.db import DBType
from simstack.util.project_root_finder import find_project_root
from simstack.util.toml_reader import TomlReader

from simstack.util.setup_logging import setup_logging
from simstack.util.mappings import ModelMappingTable, NodeMappingTable

if TYPE_CHECKING:
    from simstack.util.db import Database


def remove_password_from_connection_string(connection_string):
    parsed_url = urlparse(connection_string)

    # Extract username and rebuild netloc without password
    netloc = parsed_url.hostname
    if parsed_url.username:
        netloc = f"{parsed_url.username}@{netloc}"
    if parsed_url.port:
        netloc += f":{parsed_url.port}"

    clean_url = parsed_url._replace(netloc=netloc)

    return urlunparse(clean_url)


async def initialize_git_list(db: "Database", toml_reader: TomlReader | None):
    from simstack.models.resource_definition import GitRepo
    git_list = await db.find_all(GitRepo)
    if git_list is None and toml_reader is not None:
        git_list = toml_reader.get("parameters.common.git", [])
    return git_list


class GlobalState:
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._db = None
            cls._instance._log_handler = None
            cls._instance._path_manager = None
            cls._instance._config = None
            cls._instance._model_mappings = None
            cls._instance._node_mappings = None
            cls._instance._resource_config = None
        return cls._instance

    def __init__(self, **kwargs):
        """Initialize GlobalState instance.

        This method only runs once due to the singleton pattern.
        Use the initialize() method to set up the instance with database settings.
        """
        # We don't call self.initialize(**kwargs) here because it is async
        # and __init__ must be sync. The user should call initialize() explicitly.

        self._db = None
        self._log_handler = None
        self._path_manager = None
        self._config = None
        self._model_mappings = None
        self._node_mappings = None
        self._resource_config = None

    def __getattribute__(self, name):
        # These special attributes should always be accessible
        if name in (
            "_initialized",
            "initialize",
            "initialized",
            "_db",
            "_log_handler",
            "_path_manager",
            "_config",
            "_model_mappings",
            "_node_mappings",
            "_resource_config",
            "__class__",
            "__dict__",
        ):
            return object.__getattribute__(self, name)

        # For other attributes, check initialization
        if not object.__getattribute__(self, "_initialized"):
            raise RuntimeError(
                "GlobalState must be initialized with database settings before use"
            )

        return object.__getattribute__(self, name)

    async def initialize(self, **kwargs):
        """
        Initializes the global state with the given configuration parameters.

        Args:
            **kwargs: Arbitrary keyword arguments for configuration. The following keys are expected:
                - project_root (str, optional): The project root directory. If not provided, it will be
                  determined using `find_project_root`.
                - db_name (str, optional): The name of the database. Required when not using a TOML
                  configuration.
                - connection_string (str, optional): The connection string for the database. Required
                  when not using a TOML configuration.
                - db_type (DBType, optional): The type of the database. Required when not using a TOML
                  configuration.
                - is_test (bool, optional): Indicates whether the initialization is for testing purposes.
                  Defaults to `False`.
                - log_level (str, optional): Specifies the logging level. Defaults to `"INFO"`.
                - resource (str, optional): Specifies the resource identifier for the configuration reader.
                  Defaults to `"self"`.
                - config_file (str, optional): Specifies the path to the configuration file, defaults to simstack.toml

        Logic:
            if all values for the DB are provided in the kwargs, use the provided values
            otherwise the TOML file in the project root is used

            The project root is determined using `find_project_root`, which by default looks for a
            set of marker files, starting from the directory of find_project root and traversing up the directory tree.
            In normal runs, it should skip the project root of the simstack package
            This fails in the tests of the simstack package. The trick is to determine
            project_root manually outside the call to initialize and pass it as a kwarg.

            The TOML file decides whether the database or the file is used to set the variables
            for the specific resource

        """
        if self._initialized:
            # If already initialized, we just return if not in test mode,
            if not kwargs.get("is_test", False):
                return
        
        # In test mode, we might want to re-initialize, so we don't return
        # but we set it to True here anyway
        self._initialized = True

        project_root = kwargs.get("project_root", find_project_root())
        if project_root is None:  # maybe None was passed
            project_root = find_project_root()
        kwargs["project_root"] = project_root  # overwrite in case it was not set before
        db_name: str | None = kwargs.get("db_name", None)
        connection_string: str | None = kwargs.get("connection_string", None)
        db_type: DBType | None = kwargs.get("db_type", None)
        is_test = kwargs.get("is_test", False)
        config_file = kwargs.get("config_file", "simstack.toml")

        print(f"Initializing context with connection_string1 {connection_string} {is_test}")
        toml_reader = None
        if is_test:
            db_info = DatabaseInformation(db_name, connection_string, db_type)
        elif db_name is None or connection_string is None or db_type is None:
            # use toml
            toml_reader = TomlReader(project_root, config_file=Path(config_file))
            db_info = DatabaseInformation.from_config(toml_reader.config)

        else:
            db_info = DatabaseInformation(db_name, connection_string, db_type)

        # check that the database can be reached and set logging up
        self.initialize_database(db_info, is_test)
        self.initialize_logging(db_info.connection_string, db_info.db_name, is_test, kwargs.get("log_level", "INFO"))

        logger = logging.getLogger("Context")
        if db_info.connection_string is not None:
            safe_connection_string = remove_password_from_connection_string(
                db_info.connection_string
            )
            logger.info(
                f"Database connection to {db_type} {safe_connection_string}/{db_name}"
            )
        else:
            logger.info(f"Database connection in_memory {db_type}")
        # here we have a db, we may or may not have a toml reader
        resource_str: str = kwargs.get("resource", "self")
        # For testing, we might want to skip ConfigReader if it causes issues
        from simstack.util.config_reader import ConfigReader
        from simstack.util.resource_config import ResourceConfig
        if not kwargs.get("skip_config", False):
            try:
                self._config = await ConfigReader.create(resource_str, self._db, toml_reader, **kwargs)
                self._resource_config = ResourceConfig(project_root, resource_str)
            except Exception as e:
                if is_test:
                    logger.warning(f"Failed to initialize ConfigReader in test mode: {e}")
                else:
                    raise e

        # Initialize memory-loaded mappings
        await self.refresh_mappings()

    async def refresh_mappings(self, *, models: bool = True, nodes: bool = True):
        if models:
            self._model_mappings = await ModelMappingTable.load(self.db)
        if nodes:
            self._node_mappings = await NodeMappingTable.load(self.db)

    def initialize_logging(self, connection_string: str, db_name: str, is_test: bool, log_level: str = "INFO"):
        if is_test:
            # For tests, use simple console logging without the database handler
            # We use force=True to ensure it overrides any existing configuration (e.g. from pytest or PyCharm)
            # We also ensure the stream is sys.stderr so PyCharm/pytest can capture it properly if configured
            logging.basicConfig(
                level=log_level,
                format="%(asctime)s - %(name)-15s - %(levelname)-10s - %(filename)-20s:%(lineno)4d - %(message)s",
                stream=sys.stderr,
                force=True,
            )
            self._log_handler = logging.getLogger()
            # Flush stdout and stderr to ensure logs are not buffered
            sys.stdout.flush()
            sys.stderr.flush()
        else:
            self._log_handler = setup_logging(
                connection_string,
                db_name,
                log_level,
            )

    def initialize_database(self, db_info: DatabaseInformation, is_test: bool):
        from simstack.util.db import Database

        try:
            self._db = Database.from_db_info(db_info)
            if db_info.db_type == DBType.MONGODB:
                # Only ping real MongoDB connections
                self.db.client.admin.command("ping")

        except ConnectionError as e:
            if not is_test:
                print(f"Could not connect to the database: {e}")
                sys.exit(-1)
            else:
                # For tests, continue ignoring the database connection failure
                print(f"Warning: Database connection failed in test mode: {e}")

    @property
    def db(self):
        return self._db

    @property
    def config(self):
        return self._config


    @config.setter
    def config(self, value):
        self._config = value

    @property
    def model_mappings(self):
        return self._model_mappings

    @property
    def node_mappings(self):
        return self._node_mappings

    @property
    def resource_config(self) -> "ResourceConfig":
        return self._resource_config

    @property
    def initialized(self):
        return self._initialized



# Create the singleton instance, but it's not initialized yet
context = GlobalState()
