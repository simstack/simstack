from pathlib import Path

import pytest
import pytest_asyncio

from simstack.core.context import context
from simstack.core.definitions import DBType
from simstack.tables.model_table import make_model_table
from simstack.tables.node_table import make_node_table
from simstack.models.files import FileStack


def pytest_report_header(config):
    return "SIMSTACK: Using the Pydantic in-memory repository"


@pytest_asyncio.fixture(autouse=True, scope="session", loop_scope="session")
async def initialized_context(tmp_path_factory):
    import os

    db_connection_string = os.getenv("SIMSTACK_TEST_DB_CONNECTION_STRING", "none")
    if db_connection_string.lower() not in {"none", ""}:
        raise RuntimeError("Core tests use an isolated repository. Run MongoDB integration tests in simstack-server.")
    test_database_name = os.getenv("SIMSTACK_TEST_DB", "test_database")

    import logging

    logger = logging.getLogger("simstack.test")

    logger.info("Test context initialized with the Pydantic in-memory repository")

    working_dir = tmp_path_factory.mktemp("simstack_test")
    # set the variables such that fake dirs exist, project_root is the actual project root
    (working_dir / "home").mkdir()
    (working_dir / "home" / "simstack").mkdir()
    ssh_dir = working_dir / "home" / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").touch()

    os.environ["HOME"] = str(working_dir / "home")
    os.environ["TEMP"] = str(working_dir)

    project_root = Path.cwd() # find_project_root(skip_files=())

    import sys
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    if str(project_root / "src") not in sys.path:
        sys.path.insert(0, str(project_root / "src"))

    test_workdir = tmp_path_factory.mktemp("test_workdir")

    await context.initialize(
        console=False,
        skip_config=True, # we first need to write the ResourceDefinition to the database
        is_test=True,
        resource="self",
        connection_string="none",
        db_type=DBType.IN_MEMORY,
        db_name=test_database_name,
        workdir=working_dir,
        project_root=project_root,
        log_level="DEBUG"
    )



    from simstack.models.resource_definition import ResourceDefinition
    test_resource_definition = ResourceDefinition(
        resource_str="test",
        workdir=str(test_workdir),  # Change Path to str
        hostname="localhost",
        is_default=False,
        git_branch="main"
    )
    await context.db.save(test_resource_definition)
    # we do not need a toml reader if we pass all arguments as kwargs
    await context.initialize_configs(context.db, None, resource = "test", workdir=test_workdir,
                                     project_root=project_root, python_paths = [project_root / "src"],
                                     environment_start = "")


    # Register the test catalogue in the isolated repository.
    dirs = ["tests", "src/simstack/models" ,"src/simstack/methods"]  # "simstack/src/simstack/models", "simstack/src/simstack/methods", "tests"]
    await make_model_table(context.db, dirs=dirs, drops="src", clear=True,
                           project_root=project_root, ignore_entrypoints=True)
    await make_node_table(context.db, dirs=dirs, drops="src", clear=True,
                          project_root=project_root, ignore_entrypoints=True)

    # Refresh mappings in context after they are filled in DB
    await context.refresh_mappings()

    # Ensure a "test" resource exists in DB for tests
    from simstack.models.resource_definition import ResourceDefinition
    local_resource = ResourceDefinition(
        resource_str="test",
        hostname="localhost",
        workdir=working_dir,
        routes=[]
    )
    await context.db.save(local_resource)

    # Now re-initialize with the "local" resource after it has been saved to DB
    from simstack.util.config_reader import ConfigReader
    from unittest.mock import MagicMock
    from simstack.core.resources import allowed_resources

    # Reset allowed_resources to allow second initialization
    allowed_resources.clear_resources()


    # Mock TomlReader to avoid file access
    mock_toml = MagicMock()
    mock_toml.use_db.return_value = True
    context.config = await ConfigReader.create("test", context.db, mock_toml, project_root=project_root,
                                               workdir=working_dir, python_paths=[project_root / "src"],
                                               environment_start="")

    if hasattr(context, "log_handler") and context.log_handler:
        root_logger = context.log_handler.root
        root_logger.setLevel("ERROR")

    # Provide the initialized context
    yield context

    # Clean up the session repository and logging handlers.
    try:
        from simstack.core.resources import allowed_resources
        allowed_resources.clear_resources()
        # TODO remove route table
        try:
            from simstack.tables.node_table import route_table
            route_table.clear_routes()
        except ImportError:
            # route_table might have been removed or moved
            pass
        if context.initialized:
            # Close the repository.
            if hasattr(context, "db") and context.db:
                await context.db.close()
                context.db = None

            # Close logging handlers.
            if hasattr(context, "log_handler") and context.log_handler:
                for handler in context.log_handler.handlers[:]:
                    if hasattr(handler, "close"):
                        handler.close()
                    elif hasattr(handler, "client") and handler.client:
                        # Fallback: directly close the client
                        handler.client.close()
                    context.log_handler.removeHandler(handler)
                context.log_handler = None

            # Reset context state
            context._initialized = False
            context.model_mappings = None
            context.node_mappings = None
            context.resource_config = None
            context.config = None
            print("Test context cleaned up")
    except Exception as e:
        print(f"Warning: Error during context cleanup: {e}")


@pytest.fixture(autouse=True)
def execute_nodes_in_process_with_in_memory_repository(initialized_context, monkeypatch):
    """Keep unit execution beside its process-local repository.

    Server HTTP integration tests exercise the real separate job process.
    """
    from simstack.core.node import Node
    from simstack.core.definitions import TaskStatus

    async def execute_in_process(node):
        result = await node.execute_node_locally()
        if (
            node.registry_entry.status == TaskStatus.FAILED
            and node.registry_entry.return_kind == "bool"
        ):
            raise RuntimeError("node returned False")
        return result

    monkeypatch.setattr(Node, "run_node_as_process", execute_in_process)


@pytest.fixture(scope="function")
def document_repository(initialized_context):
    """
    Get the ODMantic engine from the context for each test.
    """
    return context.db


@pytest.fixture
def test_file_stack():
    """
    Create a temporary FileStack for testing that gets cleaned up after the test.
    """
    # Create temporary file content
    test_content = "test content"
    temp_file = context.config.workdir / "test_file.txt"
    temp_file.write_text(test_content)

    # Create FileStack
    file_stack = FileStack.from_local_file(temp_file, in_memory=True, is_hashable=True)

    yield file_stack

    # Cleanup
    if temp_file.exists():
        temp_file.unlink()
