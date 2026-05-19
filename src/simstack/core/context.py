import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse
from simstack.models.resource_definition import GitRepo
from simstack.util.database_information import DatabaseInformation
from simstack.core.definitions import DBType
from simstack.core.engine import current_engine_context
from simstack.util.project_root_finder import find_project_root
from simstack.util.toml_reader import TomlReader
from simstack.util.config_reader import ConfigReader
from simstack.util.setup_logging import setup_logging

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

    async def initialize(self, **kwargs):
        """
        Initializes the global state with the given configuration parameters.

        Raises:
            RuntimeError: If the global state is already initialized.

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
            raise RuntimeError("GlobalState already initialized")
        self._initialized = True

        project_root = kwargs.get("project_root", find_project_root())
        if project_root is None: # maybe None was passed
            project_root = find_project_root()
        kwargs["project_root"] = project_root  # overwrite in case it was not set before
        db_name : str | None = kwargs.get("db_name", None)
        connection_string: str | None = kwargs.get("connection_string", None)
        db_type: DBType | None = kwargs.get("db_type",None)
        is_test = kwargs.get("is_test", False)

        toml_reader = None
        if is_test:
            db_info = DatabaseInformation(db_name, connection_string, db_type)
        elif db_name is None or connection_string is None or db_type is None:
            # use toml
            toml_reader = TomlReader(project_root)
            db_info = DatabaseInformation.from_config(toml_reader.config)
        else:
            db_info = DatabaseInformation(db_name, connection_string, db_type)

        # check that the database can be reached and set logging up
        self.initialize_database(db_info, is_test)
        self.initialize_logging(is_test, kwargs.get("log_level", "INFO"))

        logger = logging.getLogger("Context")
        if db_info.connection_string is not None:
            safe_connection_string = remove_password_from_connection_string(db_info.connection_string)
            logger.info(f"Database connection to {db_type} {safe_connection_string}/{db_name}")
        else:
            logger.info(f"Database connection in_memory {db_type}")
        # here we have a db, we may or may not have a toml reader
        resource_str: str = kwargs.get("resource", "self")
        self.config = await ConfigReader.create(resource_str, self.db, toml_reader, **kwargs)


    def initialize_logging(self, is_test: bool, log_level: str = "INFO"):
        if is_test:
            # For tests, use simple console logging without the database handler
            logging.basicConfig(
                level=log_level,
                format="%(asctime)s - %(name)-15s - %(levelname)-10s - %(filename)-20s:%(lineno)4d - %(message)s",
            )
            self.log_handler = logging.getLogger()
        else:
            self.log_handler = setup_logging(
                self.db.connection_string,
                self.db.db_name,
                log_level,
            )

    def initialize_database(self, db_info: DatabaseInformation, is_test: bool):
        from simstack.util.db import Database, USE_REMOTE_DATABASE
        try:
            self.db = Database.from_db_info(db_info)
            if (
                not USE_REMOTE_DATABASE
                and db_info.db_type == DBType.MONGODB
                and self.db.client is not None
            ):
                # Only ping real MongoDB connections
                self.db.client.admin.command("ping")
            current_engine_context.set(self.db.engine)

        except ConnectionError as e:
            if not is_test:
                print(f"Could not connect to the database: {e}")
                sys.exit(-1)
            else:
                # For tests, continue ignoring the database connection failure
                print(f"Warning: Database connection failed in test mode: {e}")

    @property
    def initialized(self):
        return self._initialized

# Create the singleton instance, but it's not initialized yet
context = GlobalState()
