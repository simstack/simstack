import socket
from pathlib import Path
import pytest
import sys
import tempfile

from simstack.core.definitions import DBType
from simstack.core.resources import allowed_resources
from simstack.core.route_table import route_table
from simstack.models.parameters import Resource
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.config_reader import ConfigReader
from simstack.util.database_information import DatabaseInformation
from simstack.util.db import Database
import simstack.util.project_root_finder as project_root_finder
from simstack.util.project_root_finder import find_project_root
from simstack.util.toml_reader import TomlReader
from simstack.util.path_manager import path_manager


def validate_routes():
    assert route_table.targets == {'local': ['uploads'], 'self': ['local', 'uploads'], 'uploads': []}

@pytest.fixture(autouse=True)
def reset_resources():
    allowed_resources.clear_resources()
    yield
    allowed_resources.clear_resources()

class TestConfigReader:
    """Test suite for the TomlReader class."""

    @pytest.fixture
    def toml_file_path(self, monkeypatch):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Patch path_manager to use the temp_path as root
            original_root = path_manager.root_dir
            path_manager.root_dir = temp_path
            
            # Save original paths and use a copy of project_root to avoid modifying it
            original_paths = path_manager.paths.copy()
            if "project_root" in path_manager.paths:
                path_manager.paths["project_root"] = path_manager.paths["project_root"].copy()
                path_manager.paths["project_root"]["path"] = temp_path

            # Create dummy files/dirs for validation
            ssh_key_path = temp_path / "id_rsa"
            ssh_key_path.touch()
            ssh_key_str = str(ssh_key_path)
            if sys.platform == "win32":
                ssh_key_str = ssh_key_str.replace("\\", "\\\\")

            python_path = temp_path / "simstack-model"
            python_path.mkdir()

            python_path_str = str(python_path)
            if sys.platform == "win32":
                python_path_str = python_path_str.replace("\\", "\\\\")

            workdir_path = temp_path / "simstack"
            workdir_path.mkdir()
            workdir_path_str = str(workdir_path)
            if sys.platform == "win32":
                workdir_path_str = workdir_path_str.replace("\\", "\\\\")

            # Create required directory paths
            examples_path = temp_path / "examples"
            examples_path.mkdir()
            applications_path = temp_path / "applications"
            applications_path.mkdir()
            tests_path = temp_path / "tests"
            tests_path.mkdir()

            current_hostname = socket.gethostname()
            config_file = temp_path / "simstack.toml"
            config_file.write_text(f"""
[parameters]
[parameters.general]
use_db = false
git = [ "{python_path_str}"]
[parameters.db]
database = "user_data"
test_database = "user_test"
connection_string="mongodb://name:XXXXXX@mongo-server.com:27017/"
# these parameters must be adapted for each host
[resources]
allowed_resources = ["local", "self", "uploads"]
[resources.self]
ssh-key = "{ssh_key_str}"  # path to your private key
workdir = "{workdir_path_str}" # path to your simstack working directory
python_paths = [ "{python_path_str}"]
hostname = "{current_hostname}"
routes = ["local", "uploads"]
environment_start = ""
[resources.local]
ssh-key = "{ssh_key_str}"  # path to your private key
resource = "local" # resource the runner on your computer will use
workdir = "{workdir_path_str}" # path to your simstack working directory
python_paths = [ "{python_path_str}"]
hostname = "{current_hostname}"
environment_start = ""
routes = ["uploads"]
[resources.uploads]
ssh-key = "{ssh_key_str}"  # path to your private key
resource = "self" # resource the runner on your computer will used
workdir = "{workdir_path_str}" # path to your simstack working directory
python_paths = [ "{python_path_str}"]
hostname = "{current_hostname}"
[routes]
local =  ['uploads']
self =  ['local', 'uploads']
uploads =  []
[paths]
# Path configuration for the PathManager.
# Each path entry should have a path and an optional drops value.
# The path is the directory to search for Python files
# The "drops" value is a prefix to drop from module names (for import paths)
examples = {{ path = "examples", drops = "", use_pickle = false }}
applications = {{ path = "applications", drops = "", use_pickle = false }}
tests = {{ path = "tests", drops = "", use_pickle = false }}
""")
            yield temp_path

            # Restore path_manager
            path_manager.root_dir = original_root
            path_manager.paths = original_paths

    @pytest.fixture
    def toml_file_path_for_db_init(self, monkeypatch):
        with tempfile.TemporaryDirectory() as temp_dir:
            monkeypatch.setattr("simstack.util.project_root_finder.find_project_root", lambda: temp_path)
            temp_path = Path(temp_dir)
            config_file = temp_path / "simstack.toml"
            config_file.write_text(r"""
[parameters]
# these are parameters for one user for all hosts
[parameters.db]
database = "wolfgang_data"
test_database = "wolfgang_test"
[parameters.general]
use_db = true
""")
            yield temp_path

    @pytest.fixture
    def toml_reader(self,toml_file_path:Path):
        reader = TomlReader(config_path=toml_file_path)
        yield reader

    @pytest.fixture
    def resource_definitions(self, toml_reader):
        resources = ["local", "self", "uploads"]
        definitions = []
        for resource in resources:
            definition = toml_reader.get_resource_definition(resource)
            definitions.append(definition)
        yield definitions

    @pytest.fixture
    def mock_db_info(self, toml_reader):
        kwargs = { 'is_test' : True }
        toml_reader.config["parameters"]["db"]["connection_string"] = None
        db_info = DatabaseInformation.from_config(toml_reader.config, **kwargs)
        return db_info

    @pytest.fixture
    def mock_db(self, mock_db_info):
        db = Database.from_db_info(mock_db_info)

        # Patch ODMantic engine to work without sessions in test mode
        async def patched_save(instance, **kwargs):
            """Patched save method that doesn't use sessions"""
            # Use the collection directly without transactions
            collection = db.get_collection(type(instance))

            # Ensure the instance has an ObjectId
            if not instance.id:
                from odmantic import ObjectId

                instance.id = ObjectId()

            # Convert to dict and save
            doc = instance.model_dump(by_alias=True)
            doc["_id"] = instance.id

            # Recursively convert Path objects to strings for BSON encoding
            def convert_paths(obj):
                if isinstance(obj, dict):
                    return {k: convert_paths(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [convert_paths(v) for v in obj]
                elif isinstance(obj, Path):
                    return str(obj)
                return obj

            doc = convert_paths(doc)

            # Upsert the document
            await collection.replace_one({"_id": instance.id}, doc, upsert=True)
            return instance

        async def patched_save_all(instances, **kwargs):
            """Patched save_all method that doesn't use sessions"""
            results = []
            for instance in instances:
                result = await patched_save(instance, **kwargs)
                results.append(result)
            return results

        # Apply patches only for mock database
        db.save = patched_save
        db.save_all = patched_save_all

        yield db


    def test_reader(self, toml_reader):
        assert toml_reader.get("resources.allowed_resources") == ["local", "self", "uploads"]

    def test_resource_definitions(self, resource_definitions):
        assert len(resource_definitions) == 3
        assert all(isinstance(rd, ResourceDefinition) for rd in resource_definitions)
        assert resource_definitions[0].resource_str == "local"
        assert resource_definitions[1].resource_str == "self"
        assert resource_definitions[2].resource_str == "uploads"

    @pytest.mark.asyncio
    async def test_init_datasource_no_db(self,toml_reader):
        assert toml_reader.use_db() == False
        kwargs_dict = {"resource" : "local", "is_test" : False}
        project_root = find_project_root(skip_files=())
        db_info = DatabaseInformation(connection_string="mongo_db",db_name="test_db",db_type=DBType.IN_MEMORY)
        self.config_reader = await ConfigReader.create("local", db_info, toml_reader,project_root,**kwargs_dict)
        self.validate_config_reader(toml_reader)

    def validate_config_reader(self, toml_reader):
        # Path comparisons need to be careful about resolution/absolute paths
        # We just check if the name matches the one created in the fixture
        assert self.config_reader.workdir == toml_reader.get("resources.local.workdir")
        assert str(self.config_reader.python_paths[0]).endswith("simstack-model")
        assert self.config_reader.resource == Resource(value="local")
        assert self.config_reader.connection_string == "mongo_db"
        assert self.config_reader.db_name == "test_db"
        assert self.config_reader.db_type == DBType.IN_MEMORY
        validate_routes()


    @pytest.mark.asyncio
    async def test_overwrite_workdir_in_toml(self, toml_reader):
        assert toml_reader.use_db() == False

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_workdir = Path(temp_dir) / "workdir"
            temp_workdir.mkdir()

            kwargs_dict = {"resource": "local", "is_test": False, "workdir": str(temp_workdir)}

            db_info = DatabaseInformation(connection_string="mongo_db", db_name="test_db", db_type=DBType.IN_MEMORY)
            project_root = find_project_root(skip_files=())
            config_reader = await ConfigReader.create("local", db_info, toml_reader, project_root,**kwargs_dict)
            assert config_reader.workdir == temp_workdir

    @pytest.mark.asyncio
    async def test_init_datasource_with_db(self,mock_db, toml_reader, resource_definitions):
        assert mock_db.database_name == "user_data"
        toml_reader.config["parameters"]["general"]["use_db"] = True

        for resource_def in resource_definitions:
            await mock_db.save(resource_def)
        project_root = find_project_root(skip_files=())
        config_reader = await ConfigReader.create("local", mock_db, toml_reader, project_root)
        assert config_reader.db_name == "user_data"
        validate_routes()

    @pytest.mark.asyncio
    async def test_resource_property_restrictions(self, toml_reader, mock_db):
        """Test that the resource property is read-only."""
        project_root = find_project_root(skip_files=())
        config_reader = await ConfigReader.create("local", mock_db, toml_reader, project_root)

        # Test getter
        assert isinstance(config_reader.resource, Resource)
        assert config_reader.resource.value == "local"

        # Test setter raises ValueError
        with pytest.raises(ValueError, match="ConfigReader: Resource cannot be set directly"):
            config_reader.resource = "new_value"

    @pytest.mark.asyncio
    async def test_create_invalid_resource(self, toml_reader, mock_db):
        """Test ConfigReader creation with a non-existent resource."""
        # 'non_existent' is not in allowed_resources ["local", "self", "uploads"]
        project_root = find_project_root(skip_files=())
        with pytest.raises(ValueError):
            await ConfigReader.create("non_existent", mock_db, toml_reader, project_root)

    @pytest.mark.asyncio
    async def test_missing_resource_definition(self, toml_reader, mock_db):
        """Test handling of missing resource definition."""
        # Remove the resource definition from the TOML config
        del toml_reader.config["resources"]["local"]
        project_root = find_project_root(skip_files=())
        with pytest.raises(ValueError, match="Resource definition for local not found."):
            await ConfigReader.create("local", mock_db, toml_reader, project_root)

    @pytest.mark.asyncio
    async def test_validate_invalid_resource(self, toml_reader, mock_db):
        """Test validation of invalid resource names."""
        invalid_resources = ["", " ", None, "invalid!resource", "123"]
        project_root = find_project_root(skip_files=())
        for invalid_resource in invalid_resources:
            with pytest.raises(ValueError):
                await ConfigReader.create(invalid_resource, mock_db, toml_reader, project_root)

    @pytest.mark.asyncio
    async def test_override_ssh_key(self, toml_reader, mock_db):
        """Test overriding ssh_key via kwargs."""

        with tempfile.NamedTemporaryFile(delete=False) as tmp_ssh_key:
            tmp_ssh_key.write(b"test ssh key content")
            tmp_ssh_key.flush()

            kwargs = {"ssh_key": tmp_ssh_key.name}
            project_root = find_project_root(skip_files=())
            config_reader = await ConfigReader.create("local", mock_db, toml_reader, project_root,**kwargs)

            assert Path(config_reader.ssh_key).exists()
            assert config_reader.ssh_key == Path(tmp_ssh_key.name)

    @pytest.mark.asyncio
    async def test_git_list_property(self, toml_reader, mock_db):
        """Test that the git_list property is populated correctly."""
        project_root = find_project_root(skip_files=())
        config_reader = await ConfigReader.create("local", mock_db, toml_reader, project_root)

        git_list = config_reader.git_list
        assert isinstance(git_list, list)
        assert len(git_list) == 1
