import logging  # Import logging before using it
import os
import sys
from urllib.parse import urlparse, urlunparse

from simstack.core.definitions import DBType
from simstack.util.config_reader import ConfigReader
from simstack.util.path_manager import PathManager

# from simstack.core.model_table import make_models_for_path
# from simstack.core.node_table import make_nodes_for_path
from simstack.util.project_root_finder import find_project_root
from simstack.util.setup_logging import setup_logging


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


class GlobalState:
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            # TODO the DB used in simstack applications is fixed and comes from the config file, the db in the server changes by user
            cls._instance.db = None
            cls._instance.log_handler = None
            cls._instance.path_manager = None
            cls._instance.config = None

        return cls._instance

    def __init__(self, **kwargs):
        """Initialize GlobalState instance.

        This method only runs once due to the singleton pattern.
        Use the initialize() method to set up the instance with database settings.
        """
        if not hasattr(self, "_initialized"):
            self._initialized = True

            self.db = None
            self.log_handler = None
            self.path_manager = None
            self.config = None

            self.initialize(**kwargs)

    def __getattribute__(self, name):
        # These special attributes should always be accessible
        if name in ("_initialized", "initialize", "initialized"):
            return object.__getattribute__(self, name)

        # For other attributes, check initialization
        if not object.__getattribute__(self, "_initialized"):
            raise RuntimeError(
                "GlobalState must be initialized with database settings before use"
            )

        return object.__getattribute__(self, name)

    # @async_helper
    # async def remake_models_and_nodes(self,path: str):
    #     if path is not None:  # rescan all files in the path if pickling is needed
    #         parent_path = self.path_manager.find_parent_path(path)
    #         if not parent_path:
    #             raise ValueError(f"Path '{path}' not found in paths. Please check your configuration.")
    #         await make_models_for_path(parent_path, self.path_manager, context.db.engine)
    #         await make_nodes_for_path(parent_path, self.path_manager, context.db.engine)

    def initialize(self, **kwargs):
        db_name = kwargs.get("db_name", None)
        connection_string = kwargs.get("connection_string", None)
        resource_str: str = kwargs.get("resource", "self")

        """Initialize the GlobalState with database settings"""
        if self._initialized:
            raise RuntimeError("GlobalState already initialized")
        self._initialized = True

        is_test = kwargs.get("is_test", False)
        self.config = ConfigReader(
            db_name=db_name,
            connection_string=connection_string,
            resource=resource_str,
            is_test=is_test,
        )

        # check that the database can be reached and set logging up
        from simstack.util.db import Database

        # Use in-memory database for tests
        db_type = DBType.IN_MEMORY if is_test and db_name is None else DBType.MONGODB

        try:
            self.db = Database(
                db_type, self.config.database_name, self.config.connection_string
            )
            if db_type == DBType.MONGODB:
                # Only ping real MongoDB connections
                self.db.client.admin.command("ping")
        except ConnectionError as e:
            if not is_test:
                print(f"Could not connect to the database: {e}")
                sys.exit(-1)
            else:
                # For tests, continue without the database connection failure
                print(f"Warning: Database connection failed in test mode: {e}")

        if is_test:
            # For tests, use simple console logging without the database handler
            logging.basicConfig(
                level=logging.ERROR,
                format="%(asctime)s - %(name)-15s - %(levelname)-10s - %(filename)-20s:%(lineno)4d - %(message)s",
            )
            self.log_handler = logging.getLogger()
        else:
            self.log_handler = setup_logging(
                self.config.connection_string,
                self.config.database_name,
                kwargs.get("log_level", "INFO"),
            )

        # initialize the rest of the variables in the config, but now we can get the errors in the database
        self.config.secondary_init(kwargs.get("workdir", None))

        logger = logging.getLogger("Context")
        safe_connection_string = remove_password_from_connection_string(
            self.config.connection_string
        )
        logger.info(
            f"Database connection established to {self.config.database_name} at {safe_connection_string}"
        )

        # Initialize PathManager from config
        self.path_manager = PathManager.from_config(self.config)

        # Set any additional parameters
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    @property
    def initialized(self):
        return self._initialized


root_dir = find_project_root()
path_dir = os.path.join(root_dir, "src")
if path_dir not in os.sys.path:
    os.sys.path.append(path_dir)

# Create the singleton instance, but it's not initialized yet
context = GlobalState()
