from pathlib import Path

import pytest
import pytest_asyncio

from simstack.core.context import context
from simstack.core.definitions import DBType
from simstack.models import ModelMapping
from simstack.tables.model_table import make_model_table
from simstack.tables.node_table import make_node_table
from simstack.models.files import FileStack


def pytest_report_header(config):
    import os
    db_connection_string = os.getenv("SIMSTACK_TEST_DB_CONNECTION_STRING", "none")
    use_real_db = db_connection_string.lower() != "none"

    if use_real_db:
        conn_str = db_connection_string if db_connection_string.lower() != "none" else os.getenv("MONGODB_CONNECTION_STRING", "mongodb://localhost:27017")
        return f"SIMSTACK: Using real MongoDB database at: {conn_str}"
    else:
        return "SIMSTACK: Using mock database (patched for mongomock)"

@pytest_asyncio.fixture(autouse=True, scope="session", loop_scope="session")
async def initialized_context(tmp_path_factory):
    # Use environment variable to control the database type for tests
    import os

    db_connection_string = os.getenv("SIMSTACK_TEST_DB_CONNECTION_STRING", "none")
    use_real_db = db_connection_string.lower() != "none" and db_connection_string != ""
    test_database_name = os.getenv("SIMSTACK_TEST_DB", "test_database")

    import logging
    logging.getLogger("pymongo").setLevel(logging.WARNING)

    logger = logging.getLogger("simstack.test")

    if use_real_db:
        logger.info(f"Test context initialized with real MongoDB")
        if not _mongodb_available(db_connection_string):
            pytest.exit("SIMSTACK_TEST: Failed to reach MongoDB. Terminating all tests. .",
                        returncode=1)
        # Test actual read/write operations
        if not await _test_mongodb_connection(db_connection_string, test_database_name):
            pytest.exit("SIMSTACK_TEST: Failed to write and read test document from MongoDB. Terminating all tests.",
                        returncode=1)
    else:
        logger.info("Test context initialized with mock database (patched for mongomock)")

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
        connection_string=db_connection_string if use_real_db else "none",
        db_type=DBType.MONGODB if use_real_db else DBType.IN_MEMORY,
        db_name=test_database_name,
        workdir=working_dir,
        project_root=project_root,
        log_level="DEBUG"
    )



    if use_real_db:
        await context.db.reset_database()
    else:
        await create_db_patches(context.db)

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


    # Initialize model and node tables for both real and mock databases
    dirs = ["tests", "src/simstack/models" ,"src/simstack/methods"]  # "simstack/src/simstack/models", "simstack/src/simstack/methods", "tests"]
    await make_model_table(context.db, dirs=dirs, drops="src", clear=True,
                           project_root=project_root, ignore_entrypoints=True)
    await make_node_table(context.db, dirs=dirs, drops="src", clear=True,
                          project_root=project_root, ignore_entrypoints=True)

    # Refresh mappings in context after they are filled in DB
    if not use_real_db:
        await context.refresh_mappings()

    test_mapping = await context.db.find_one(ModelMapping, ModelMapping.name == "FloatData")
    test_node_mapping = await context.db.find_one(ModelMapping, ModelMapping.name == "failing_node")
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

    if use_real_db:
        # print(f"\n[SIMSTACK] Test context initialized with real MongoDB database at: {db_connection_string}")
        pass
    else:
        # print("\n[SIMSTACK] Test context initialized with mock database (patched for mongomock)")
        pass

    import logging
    logger = logging.getLogger("simstack.test")
    if use_real_db:
        logger.info(f"Test context initialized with real MongoDB database at: {db_connection_string}")
    else:
        logger.info("Test context initialized with mock database (patched for mongomock)")

    if hasattr(context, "log_handler") and context.log_handler:
        root_logger = context.log_handler.root
        root_logger.setLevel("ERROR")

    # Provide the initialized context
    yield context

    # Cleanup after each test
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
            # Close the main database connection
            if hasattr(context, "db") and context.db:
                await context.db.close()
                context.db = None

            # Close logging handler's MongoDB connection
            if hasattr(context, "log_handler") and context.log_handler:
                # Close all handlers that might have MongoDB connections
                for handler in context.log_handler.handlers[:]:
                    if hasattr(handler, "close"):
                        # This is likely a DBLogHandler with a close method
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
def execute_nodes_in_process_with_mongomock(initialized_context, monkeypatch):
    """Keep unit tests on the process-local in-memory database.

    Production and real-MongoDB integration tests exercise ``run_node`` in a
    subprocess. Mongomock cannot expose its state to a child process, so these
    tests retain their existing execution assertions in the current process.
    """
    import os

    connection_string = os.getenv("SIMSTACK_TEST_DB_CONNECTION_STRING", "none")
    if connection_string and connection_string.lower() != "none":
        return

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


async def create_db_patches(db):
    from simstack.util.db import Database
    # Patch ODMantic engine to work without sessions in test mode
    async def patched_save(instance, *args, **kwargs):
        """Patched save method that doesn't use sessions"""
        # Handle engine.save(None, model) which is how it might be called
        if instance is None and args:
            instance = args[0]
            args = args[1:]

        # If instance is still None, it might be a call to validate args
        if instance is None:
            from odmantic import Model
            if not args or not isinstance(args[0], Model):
                raise TypeError("AIOEngine.save() missing 1 required positional argument: 'instance'")
            return instance

        # Use the collection directly without transactions
        collection = context.db.get_collection(type(instance))

        # Ensure the instance has an ObjectId
        if not instance.id:
            from odmantic import ObjectId

            instance.id = ObjectId()

        # Convert to dict and save
        doc = instance.model_dump(by_alias=True)
        doc["_id"] = instance.id

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

    # Apply patches only for the mock database
    db._engine.save = patched_save
    db._engine.save_all = patched_save_all
    # DO NOT patch find/find_one on _engine with the facade methods,
    # because the facade methods call _engine.find/find_one, creating recursion.

    db.save = Database.save.__get__(context.db, Database)
    db.find = Database.find.__get__(context.db, Database)
    db.find_one = Database.find_one.__get__(context.db, Database)
    db.save_all = patched_save_all
    db.save_unchecked = Database.save_unchecked.__get__(context.db, Database)

    # Mock database command for stats
    async def patched_command(command, *args, **kwargs):
        if isinstance(command, str):
            command = {command: 1}
        if "dbStats" in command:
            return {"db": "ui_testing", "collections": 10, "objects": 100}
        if "ping" in command:
            return {"ok": 1.0}
        raise NotImplementedError(f"Mock command {command} not implemented")

    context.db.database.command = patched_command


@pytest.fixture(scope="function")
def odmantic_engine(initialized_context):
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

# Check if real MongoDB is available for tests that require it
def _mongodb_available(conn_str: str = None):
    """Check if MongoDB is available"""
    try:
        import os
        from urllib.parse import urlparse
        parsed = urlparse(conn_str)
        host = parsed.hostname or "localhost"
        port = parsed.port or 27017

        # Use a simple socket connection test instead of Motor client
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)  # 1 second timeout
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


async def _test_mongodb_connection(conn_str: str, db_name: str = "ui_testing"):
    """Test MongoDB connection by writing and reading a test document"""
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        import datetime

        client = AsyncIOMotorClient(conn_str)
        db = client.get_database(db_name)
        collection = db["connection_test"]

        # Write test document
        test_doc = {
            "test": "connection_check",
            "timestamp": datetime.datetime.now(datetime.UTC)
        }
        result = await collection.insert_one(test_doc)

        # Read it back
        retrieved_doc = await collection.find_one({"_id": result.inserted_id})

        # Clean up
        await collection.delete_one({"_id": result.inserted_id})
        client.close()

        if retrieved_doc is None or retrieved_doc.get("test") != "connection_check":
            return False

        return True
    except Exception as e:
        print(f"MongoDB connection test failed: {e}")
        return False
