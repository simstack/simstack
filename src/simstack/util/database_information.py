import sys
from typing import Dict, Any, Union
from simstack.core.definitions import DBType


class DatabaseInformation:
    """
    Represents a database and its connection information.

    This class encapsulates essential details about a database, including its name and
    connection string. It provides a convenient method for initializing this information
    from a configuration file, facilitating streamlined database setups and connections.

    Attributes:
        _db_name (str): The name of the database.
        _connection_string (str): The connection string used to access the database.

    Methods:
        from_config_file(config, **kwargs):
            Initialize DatabaseInformation from TOML config or a database specified in TOML.
    """

    def __init__(
        self, db_name: str, connection_string: str, db_type: DBType = DBType.MONGODB
    ):
        self._db_name = db_name
        self._connection_string = connection_string
        self._db_type = db_type

    @classmethod
    def from_config(cls, config: Dict[str, Any], **kwargs):
        """
        Initialize DatabaseInformation from TOML config
        kwargs override config file.
        """
        common_params = config.get("parameters", {}).get("db", {})
        is_test = kwargs.get("is_test", False)

        # Standard initialization from TOML
        db_name = kwargs.get("db_name")
        # for testing we can use an in_memory db
        if db_name is None:
            # the package simstack.toml has no db_name and connections string
            db_name = common_params.get("database")
            if db_name is None:
                print(
                    "You must specify a database name in parameters.db in the config file"
                )
                sys.exit(-1)

        connection_string = kwargs.get("connection_string")
        if connection_string is None:
            connection_string = common_params.get("connection_string")

        if not is_test and connection_string is None:
            print("You must specify a connection string in the config file")
            sys.exit(-1)

        # Use in-memory database for tests
        db_type = (
            DBType.IN_MEMORY
            if is_test and connection_string is None
            else DBType.MONGODB
        )

        return cls(
            db_name=db_name, connection_string=connection_string, db_type=db_type
        )

    @classmethod
    def from_db_info(cls, db_info: "DatabaseInformation"):
        return cls(db_info._db_name, db_info._connection_string, db_info._db_type)

    @classmethod
    def from_db_info_or_db(cls, db_info: Union["DatabaseInformation", "Database"]):
        from simstack.util.db import Database

        if isinstance(db_info, DatabaseInformation):
            return DatabaseInformation.from_db_info(db_info)
        elif isinstance(db_info, Database):
            return cls(db_info.database_name, "None", db_info.databae_type)
        else:
            raise TypeError(
                "db_info must be an instance of DatabaseInformation or Database"
            )

    @property
    def db_type(self) -> DBType:
        return self._db_type

    @property
    def db_name(self) -> str:
        return self._db_name

    @property
    def connection_string(self) -> str:
        return self._connection_string

    def __repr__(self) -> str:
        return f"DatabaseInformation(db_name='{self._db_name}', connection_string='{self._connection_string}', db_type={self._db_type})"

    def get_information(self):
        """
        Returns a tuple of the initialization parameters that can be used as *args for __init__.

        Returns:
            tuple: (db_name, connection_string, db_type)
        """
        return self._db_name, self._connection_string, self._db_type
