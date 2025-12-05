import pytest
from unittest.mock import MagicMock
from pathlib import Path

from simstack.core.definitions import DBType
from simstack.util.config_reader import ConfigReader
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation

@pytest.fixture
def mock_db_info():
    db_info = DatabaseInformation("user_db","mongodb://localhost:27017/user_db",
                                  db_type=DBType.MONGODB)
    return db_info

@pytest.fixture
def mock_toml_reader():
    reader = MagicMock(spec=TomlReader)
    reader.config = {}
    reader.get.side_effect = lambda key, default=None: default
    return reader

@pytest.fixture
def mock_resource_def():
    rd = MagicMock(spec=ResourceDefinition)
    rd.dict.return_value = {
        "resource": "test_resource",
        "type": "slurm",
        # Add other required fields for ResourceDefinition if strict validation is on
    }
    rd.name = "test_resource"
    return rd

@pytest.mark.asyncio
class TestConfigReader:

    async def test_create_from_database(self, mock_db_info, mock_toml_reader, mock_resource_def):
        """Test initializing ConfigReader when resources are in the database."""
        # Setup DB responses
        mock_db_info.find_all.side_effect = [
            [mock_resource_def],  # First call for ResourceDefinition
            []                    # Second call for GitRepo
        ]
        
        # Execute
        reader = await ConfigReader.create(
            db=mock_db_info,
            toml_reader=mock_toml_reader,
            resource="test_resource"
        )

        # Verify
        assert reader.allowed_resources == ["test_resource"]
        assert reader.resource.resource == "test_resource"
        mock_db_info.find_all.assert_any_call(ResourceDefinition)

    async def test_create_from_toml(self, mock_db_info, mock_toml_reader):
        """Test initializing ConfigReader when resources are in TOML (DB empty)."""
        # Setup DB to return None for ResourceDefinition (simulating empty/fail)
        mock_db_info.find_all.side_effect = [None, None]

        # Setup TOML content
        def toml_get_side_effect(key, default=None):
            if key == "parameters.common.allowed_resources":
                return ["local_resource"]
            if key == "parameters.local_resource":
                return {"resource": "local_resource", "type": "local"}
            if key == "parameters.common.git":
                return []
            return default

        mock_toml_reader.get.side_effect = toml_get_side_effect
        mock_toml_reader.config = {}

        # Execute
        reader = await ConfigReader.create(
            db=mock_db_info,
            toml_reader=mock_toml_reader,
            resource="local_resource"
        )

        # Verify
        assert reader.allowed_resources == ["local_resource"]
        assert reader.resource.resource == "local_resource"

    async def test_create_fails_missing_resource_arg(self, mock_db_info, mock_toml_reader):
        """Test that create fails if resource kwarg is missing."""
        with pytest.raises(ValueError, match="Resource must be specified"):
            await ConfigReader.create(db=mock_db_info, toml_reader=mock_toml_reader)

    async def test_create_fails_resource_not_allowed(self, mock_db_info, mock_toml_reader, mock_resource_def):
        """Test validation when requested resource is not in allowed list."""
        mock_db_info.find_all.side_effect = [[mock_resource_def], []]

        with pytest.raises(ValueError, match="not found in the list of allowed resources"):
            await ConfigReader.create(
                db=mock_db_info,
                toml_reader=mock_toml_reader,
                resource="invalid_resource"
            )

    async def test_init_routes_and_paths(self, mock_db_info, mock_resource_def):
        """Test route parsing and path properties in __init__ logic."""
        # Create a real TomlReader mock with data
        toml = MagicMock(spec=TomlReader)
        toml.config = {
            "routes": [
                {"source": "A", "target": "B", "host": "h1"}
            ]
        }
        # Mock get behavior
        data = {
            "parameters.common.docker": True,
            "parameters.common.source_dir": "/tmp/src",
            "server.secret_key": "supersecret"
        }
        toml.get.side_effect = lambda k, d=None: data.get(k, d)

        # Init directly
        reader = ConfigReader(
            db_nfo=mock_db_info,
            resource_definition=mock_resource_def,
            allowed_resources=["test_resource"],
            git_list=[],
            toml_reader=toml,
            resource="test_resource" # kwargs
        )

        # Checks
        assert reader.docker is True
        assert reader.external_source_dir == Path("/tmp/src")
        
        # Route checking
        route = reader.get_route("A", "B")
        assert route["host"] == "h1"
        
        empty_route = reader.get_route("A", "C")
        assert empty_route == []

    async def test_init_fails_bad_routes(self, mock_db_info, mock_resource_def):
        """Test that malformed routes raise ValueError."""
        toml = MagicMock(spec=TomlReader)
        toml.config = {
            "routes": [{"source": "A"}] # Missing keys
        }
        toml.get.return_value = None

        with pytest.raises(ValueError, match="does not contain 'source', 'target', 'host' keys"):
            ConfigReader(
                db_nfo=mock_db_info,
                resource_definition=mock_resource_def,
                allowed_resources=["test_resource"],
                git_list=[],
                toml_reader=toml
            )

    async def test_init_fails_docker_no_source(self, mock_db_info, mock_resource_def):
        """Test validation failure when docker is True but source_dir is NONE."""
        toml = MagicMock(spec=TomlReader)
        toml.config = {}
        
        def get_side_effect(key, default=None):
            if key == "parameters.common.docker": return True
            if key == "parameters.common.source_dir": return "NONE"
            return default
        toml.get.side_effect = get_side_effect

        with pytest.raises(ValueError, match="must specify an external source directory"):
            ConfigReader(
                db_nfo=mock_db_info,
                resource_definition=mock_resource_def,
                allowed_resources=["test_resource"],
                git_list=[],
                toml_reader=toml
            )
