from unittest.mock import patch

from simstack.core.definitions import DBType
from simstack.util.database_information import DatabaseInformation


class TestDatabaseInformation:
    def test_from_config_with_valid_config(self):
        config = {
            "parameters": {
                "common": {
                    "database": "test_db",
                    "connection_string": "mongodb://test_connection_string"
                }
            }
        }
        kwargs = {}
        db_info = DatabaseInformation.from_config(config, **kwargs)

        assert db_info.db_name == "test_db"
        assert db_info.connection_string == "mongodb://test_connection_string"
        assert db_info.db_type == DBType.MONGODB

    def test_from_config_with_kwargs_override(self):
        config = {
            "parameters": {
                "common": {
                    "database": "test_db",
                    "connection_string": "mongodb://test_connection_string"
                }
            }
        }
        kwargs = {"db_name": "override_db", "connection_string": "override_connection_string"}
        db_info = DatabaseInformation.from_config(config, **kwargs)

        assert db_info.db_name == "override_db"
        assert db_info.connection_string == "override_connection_string"
        assert db_info.db_type == DBType.MONGODB

    def test_from_config_with_in_memory_db(self):
        config = {}
        kwargs = {"is_test": True}
        db_info = DatabaseInformation.from_config(config, **kwargs)

        assert db_info.db_name is None
        assert db_info.connection_string is None
        assert db_info.db_type == DBType.IN_MEMORY

    def test_from_config_missing_db_name(self):
        config = {
            "parameters": {
                "common": {}
            }
        }
        kwargs = {}
        with patch("sys.exit") as mock_exit:
            DatabaseInformation.from_config(config, **kwargs)
            mock_exit.assert_called_once_with(-1)

    def test_from_config_missing_connection_string(self):
        config = {
            "parameters": {
                "common": {
                    "database": "test_db"
                }
            }
        }
        kwargs = {}
        with patch("sys.exit") as mock_exit:
            DatabaseInformation.from_config(config, **kwargs)
            mock_exit.assert_called_once_with(-1)
