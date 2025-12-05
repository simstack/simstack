from pathlib import Path
import pytest
import tempfile

from simstack.core.definitions import DBType
from simstack.util.config_reader import ConfigReader
from simstack.util.database_information import DatabaseInformation
from simstack.util.toml_reader import TomlReader

class TestTomlReader:
    """Test suite for the TomlReader class."""

    @pytest.fixture
    def toml_file_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_file = temp_path / "simstack.toml"
            config_file.write_text(r"""
[parameters]
# these are parameters for one user for all hosts
[parameters.common]
allowed_resources = ["local", "self", "remote", "uploads"]
database = "wolfgang_data"
test_database = "wolfgang_test"
use_db = false
connection_string="mongodb://name:XXXXXX@mongo-server.com:27017/"
git = [ "C:\\Users\\XXX\\PyCharmProjects\\simstack-model"]
# these parameters must be adapted for each host
[parameters.self]
ssh-key = "C:\\Users\\XXX\\Documents\\etc\\.ssh\\openssh"  # path to your private key
resource = "local" # resource the runner on your computer will use
workdir = "C:\\Users\\XXX\\simstack" # path to your simstack working directory
python_path = [ "C:\\Users\\XXX\\PyCharmProjects\\simstack-model"]
environment_start = ""
[parameters.local]
ssh-key = "C:\\Users\\XXX\\Documents\\etc\\.ssh\\openssh"  # path to your private key
resource = "local" # resource the runner on your computer will use
workdir = "C:\\Users\\XXX\\simstack" # path to your simstack working directory
python_path = [ "C:\\Users\\XXX\\PyCharmProjects\\simstack-model"]
environment_start = ""
[parameters.uploads]
ssh-key = "C:\\Users\\bj7610\\Documents\\etc\\.ssh\\surface11_openssh"  # path to your private key
resource = "self" # resource the runner on your computer will used
workdir = "C:\\Users\\bj7610\\simstack" # path to your simstack working directory
python_path = [ "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model",
               "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model\\src"]
[parameters.remote]
ssh-key = "/home/remote_user/.ssh/id_rsa"  # path to your private key
workdir = "/home/remote_user/simstack" # path to your simstack working directory
python_path = [ "/home/remote_user/projects/simstack-model"]
environment_start = "conda activate simstack-env"
# normal users do not have to change anything below this line
# these are the parameters for the overall configurations
[hosts]
local = "localhost"
remote="remote.int.kit.edu"
justus="justus.int.kit.edu"
horeka="horeka.int.kit.edu"

[[routes]]
source = "local"
target = "remote"
host = "local"

[[routes]]
source = "remote"
target = "local"
host = "local"
[paths]
# Path configuration for the PathManager.
# Each path entry should have a path and an optional drops value.
# The path is the directory to search for Python files
# The "drops" value is a prefix to drop from module names (for import paths)
#examples = { path = "examples", drops = "", use_pickle = false }
#applications = { path = "applications", drops = "", use_pickle = false }
#tests = { path = "tests", drops = "", use_pickle = false }
""")
            yield temp_path

    @pytest.fixture
    def toml_reader(self,toml_file_path:Path):
        reader = TomlReader(config_path=toml_file_path)
        yield reader

    def test_reader(self,toml_reader):
        assert toml_reader.get("parameters.common.resources") == ["local", "self", "remote", "uploads"]

    @pytest.mark.asyncio
    async def test_init_datasource_no_db(self,toml_reader):
        assert toml_reader.get("parameters.common.use_db") == False
        kwargs_dict = {"resource" : "local", "is_test" : False}

        db_info = DatabaseInformation(connection_string="mongo_db",db_name="test_db",db_type=DBType.IN_MEMORY)
        config_reader = await ConfigReader.create("local", db_info, toml_reader,**kwargs_dict)

        assert config_reader.workdir == Path("C:\\Users\\XXX\\simstack-model")
        assert config_reader.python_path == [r"C:\\Users\\XXX\\PyCharmProjects\\simstack-model"]
        assert config_reader.resource.resource == "local"
        assert config_reader.connection_string == "mongo_db"
        assert config_reader.db_name == "test_db"
        assert config_reader.db_type == DBType.IN_MEMORY

    #
    # def test_initialize_with_default_path(self, mocker):
    #     """Test initialization with the default path and file."""
    #     mocked_find_project_root = mocker.patch(
    #         "simstack.toml_reader.find_project_root", return_value=Path("/mock/root")
    #     )
    #     mocked_tomllib_load = mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key": "value"})
    #     mocked_open = mocker.patch("builtins.open", mocker.mock_open(read_data="mock data"))
    #
    #     reader = TomlReader()
    #
    #     mocked_find_project_root.assert_called_once()
    #     mocked_tomllib_load.assert_called_once()
    #     mocked_open.assert_called_once_with(Path("/mock/root/simstack.toml"), "rb")
    #     assert reader.config == {"key": "value"}
    #
    # def test_initialize_with_custom_path(self, mocker):
    #     """Test initialization with a custom path and file."""
    #     mocked_tomllib_load = mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key": "value"})
    #     mocked_open = mocker.patch("builtins.open", mocker.mock_open(read_data="mock data"))
    #
    #     custom_path = Path("/custom/path")
    #     custom_file = "custom.toml"
    #     reader = TomlReader(config_path=custom_path, config_file=custom_file)
    #
    #     mocked_open.assert_called_once_with(custom_path / custom_file, "rb")
    #     mocked_tomllib_load.assert_called_once()
    #     assert reader.config == {"key": "value"}
    #
    # def test_initialize_file_not_found(self, mocker):
    #     """Test behavior when the TOML file is not found."""
    #     mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
    #
    #     with pytest.raises(SystemExit) as excinfo:
    #         TomlReader(config_file="non_existent.toml")
    #     assert excinfo.value.code == -1
    #
    # def test_initialize_invalid_toml(self, mocker):
    #     """Test behavior when the TOML file contains invalid syntax."""
    #     mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
    #     mocker.patch("builtins.open", mocker.mock_open(read_data="invalid toml"))
    #     mocker.patch("simstack.toml_reader.tomllib.load", side_effect=tomllib.TOMLDecodeError("error", "doc", 1))
    #
    #     with pytest.raises(SystemExit) as excinfo:
    #         TomlReader()
    #     assert excinfo.value.code == -1
    #
    # def test_get_existing_key(self, mocker):
    #     """Test retrieving an existing key."""
    #     mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
    #     mocker.patch("builtins.open", mocker.mock_open(read_data="mock"))
    #     mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key1": {"key2": "value"}})
    #
    #     reader = TomlReader()
    #     assert reader.get("key1.key2") == "value"
    #
    # def test_get_non_existing_key(self, mocker):
    #     """Test retrieving a non-existing key."""
    #     mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
    #     mocker.patch("builtins.open", mocker.mock_open(read_data="mock"))
    #     mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key1": {"key2": "value"}})
    #
    #     reader = TomlReader()
    #     assert reader.get("key1.key3") is None
    #
    # def test_get_with_default_value(self, mocker):
    #     """Test retrieving a non-existing key with a default value."""
    #     mocker.patch("simstack.toml_reader.find_project_root", return_value=Path("/mock/root"))
    #     mocker.patch("builtins.open", mocker.mock_open(read_data="mock"))
    #     mocker.patch("simstack.toml_reader.tomllib.load", return_value={"key1": {"key2": "value"}})
    #
    #     reader = TomlReader()
    #     assert reader.get("key1.key3", default="default_value") == "default_value"
