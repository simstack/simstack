import pytest
import logging
import sys
from unittest.mock import AsyncMock, patch, MagicMock
from types import SimpleNamespace
from simstack.core.context import GlobalState, context
from simstack.util.db import DBType

@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset GlobalState singleton before and after each test."""
    GlobalState._instance = None
    GlobalState._initialized = False
    yield
    GlobalState._instance = None
    GlobalState._initialized = False

@pytest.mark.asyncio
async def test_singleton_behavior():
    # Since 'context' is imported at module level, it might already be an instance.
    # We want to ensure that ANY GlobalState() call returns the same instance as 'context'
    # IF we haven't cleared the singleton yet.
    # But reset_global_state clears GlobalState._instance.
    # This means 'context' (which was created when context.py was first imported)
    # might be a different object than a newly created GlobalState() if we reset it.
    
    # To properly test singleton within one session where 'context' already exists:
    gs1 = GlobalState()
    gs2 = GlobalState()
    assert gs1 is gs2

@pytest.mark.asyncio
async def test_initialization_flow(tmp_path):
    gs = GlobalState()
    assert gs.initialized is False
    
    # Try accessing property before initialization should raise RuntimeError
    with pytest.raises(RuntimeError, match="GlobalState must be initialized"):
        _ = gs.db

    # Create simstack.toml in tmp_path
    toml_file = tmp_path / "simstack.toml"
    toml_file.write_text("""
[parameters.db]
database = "test_db"
connection_string = "mongodb://localhost:27017"
[parameters.general]
use_db = true
[resources.test]
hostname = "localhost"
workdir = "test_workdir"
""")

    # Mock dependencies
    with patch("simstack.core.context.DatabaseInformation") as mock_db_info_class, \
         patch("simstack.util.db.Database") as mock_db_class, \
         patch("simstack.util.config_reader.ConfigReader.create", new_callable=AsyncMock) as mock_config_create, \
         patch("simstack.core.context.setup_logging") as mock_setup_logging, \
         patch("simstack.util.mappings.ModelMappingTable.load", new_callable=AsyncMock) as mock_model_load, \
         patch("simstack.util.mappings.NodeMappingTable.load", new_callable=AsyncMock) as mock_node_load:
        
        from simstack.models.resource_definition import ResourceDefinition
        mock_resource_definition = ResourceDefinition(
            resource_str="test",
            hostname="localhost",
            workdir=str(tmp_path / "workdir"),
            python_paths=[],
            routes=[]
        )

        mock_db_info = MagicMock()
        mock_db_info.connection_string = "mongodb://localhost:27017"
        mock_db_info.db_name = "test_db"
        mock_db_info.db_type = DBType.MONGODB
        mock_db_info_class.return_value = mock_db_info

        mock_db_instance = MagicMock()
        mock_db_instance.connection_string = "mongodb://localhost:27017"
        mock_db_instance.db_name = "test_db"
        mock_db_instance.find = AsyncMock(return_value=[mock_resource_definition])
        mock_db_class.from_db_info.return_value = mock_db_instance
        
        mock_config_instance = MagicMock()
        mock_config_create.return_value = mock_config_instance
        
        await gs.initialize(
            is_test=True,
            project_root=tmp_path,
            db_name="test_db",
            connection_string="mongodb://localhost:27017",
            db_type=DBType.MONGODB,
            resource="test"
        )
        
        assert gs.initialized is True
        assert gs.in_docker is False
        assert gs.current_node_name is None
        assert gs.db is mock_db_instance
        assert gs.config is mock_config_instance
        assert gs.resource_config is not None
        
        # Verify read-only properties (no setters)
        with pytest.raises(AttributeError):
            gs.db = None
        with pytest.raises(AttributeError):
            gs.resource_config = None

        gs.in_docker = True
        gs.current_node_name = "multistep_optimizer"
        assert gs.in_docker is True
        assert gs.current_node_name == "multistep_optimizer"


@pytest.mark.asyncio
async def test_initialize_persists_in_docker_flag(tmp_path):
    gs = GlobalState()
    toml_file = tmp_path / "simstack.toml"
    toml_file.write_text("""
[parameters.db]
database = "test_db"
connection_string = "mongodb://localhost:27017"
[parameters.general]
use_db = true
[resources.test]
hostname = "localhost"
workdir = "test_workdir"
""")

    with patch("simstack.core.context.DatabaseInformation") as mock_db_info_class, \
         patch("simstack.util.db.Database") as mock_db_class, \
         patch("simstack.util.config_reader.ConfigReader.create", new_callable=AsyncMock), \
         patch("simstack.core.context.setup_logging"), \
         patch("simstack.util.mappings.ModelMappingTable.load", new_callable=AsyncMock), \
         patch("simstack.util.mappings.NodeMappingTable.load", new_callable=AsyncMock):

        mock_db_info = MagicMock()
        mock_db_info.connection_string = "mongodb://localhost:27017"
        mock_db_info.db_name = "test_db"
        mock_db_info.db_type = DBType.MONGODB
        mock_db_info_class.return_value = mock_db_info
        mock_db_class.from_db_info.return_value = MagicMock()

        await gs.initialize(
            is_test=True,
            project_root=tmp_path,
            db_name="test_db",
            connection_string="mongodb://localhost:27017",
            db_type=DBType.MONGODB,
            resource="test",
            in_docker=True,
        )

        assert gs.in_docker is True


@pytest.mark.asyncio
async def test_getattribute_whitelist():
    gs = GlobalState()
    # These should NOT raise RuntimeError even if not initialized
    assert gs._initialized is False
    assert gs.initialized is False
    assert gs._in_docker is False
    assert gs._current_node_name is None
    
    # Let's check an attribute NOT in the whitelist.
    with pytest.raises(RuntimeError):
        _ = gs.some_random_attribute

@pytest.mark.asyncio
async def test_reinitialization_behavior(tmp_path):
    gs = GlobalState()
    
    # Create simstack.toml in tmp_path
    toml_file = tmp_path / "simstack.toml"
    toml_file.write_text("""
[parameters.db]
database = "test_db"
connection_string = "mongodb://localhost:27017"
[parameters.general]
use_db = true
[resources.self]
hostname = "localhost"
workdir = "test_workdir"
""")

    with patch("simstack.core.context.DatabaseInformation") as mock_db_info_class, \
         patch("simstack.util.db.Database"), \
         patch("simstack.util.config_reader.ConfigReader.create", new_callable=AsyncMock), \
         patch("simstack.core.context.setup_logging"), \
         patch("simstack.util.mappings.ModelMappingTable.load", new_callable=AsyncMock), \
         patch("simstack.util.mappings.NodeMappingTable.load", new_callable=AsyncMock):
        
        mock_db_info = MagicMock()
        mock_db_info.connection_string = "mongodb://localhost:27017"
        mock_db_info.db_name = "test_db"
        mock_db_info.db_type = DBType.MONGODB
        mock_db_info_class.from_config.return_value = mock_db_info
        mock_db_info_class.return_value = mock_db_info

        await gs.initialize(is_test=True, project_root=tmp_path)
        assert gs.initialized is True
        
        # Second call to initialize should return early if not is_test or if already initialized
        with patch.object(gs, 'initialize_database') as mock_init_db:
            await gs.initialize(is_test=False, project_root=tmp_path)
            mock_init_db.assert_not_called()
