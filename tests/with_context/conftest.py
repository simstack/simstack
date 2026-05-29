import pytest
import pytest_asyncio

from simstack.core.context import context
from simstack.core.definitions import DBType
from simstack.tables.model_table import make_model_table
from simstack.tables.node_table import make_node_table
from simstack.models.files import FileStack
from simstack.util.project_root_finder import find_project_root

@pytest.fixture(scope="session")
def event_loop():
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    loop.close()

def pytest_report_header(config):
    import os
    db_connection_string = os.getenv("SIMSTACK_TEST_DB_CONNECTION_STRING", "none").lower()
    use_real_db = db_connection_string != "none"
    
    if use_real_db:
        conn_str = db_connection_string if db_connection_string != "none" else os.getenv("MONGODB_CONNECTION_STRING", "mongodb://localhost:27017")
        return f"SIMSTACK: Using real MongoDB database at: {conn_str}"
    else:
        return "SIMSTACK: Using mock database (patched for mongomock)"

@pytest_asyncio.fixture(autouse=True, scope="session")
async def initialized_context(tmp_path_factory, event_loop):
    # Use environment variable to control the database type for tests
    import os

    db_connection_string = os.getenv("SIMSTACK_TEST_DB_CONNECTION_STRING", "none").lower()
    use_real_db = db_connection_string != "none"

    if use_real_db and not _mongodb_available(db_connection_string):
        raise RuntimeError(f"fSIMSTACK_TEST cannot reach db at: {db_connection_string}")
    else:
        print("Test context initialized with mock database (patched for mongomock)")

    if use_real_db:
        print(f"Test context initialized with real MongoDB database")
    else:
        print("Test context initialized with mock database (patched for mongomock)")

    working_dir = tmp_path_factory.mktemp("simstack_test")
    # set the variables such that fake dirs exist, project_root is the actual project root
    (working_dir / "home").mkdir()
    (working_dir / "home" / "simstack").mkdir()
    ssh_dir = working_dir / "home" / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").touch()

    os.environ["HOME"] = str(working_dir / "home")
    os.environ["TEMP"] = str(working_dir)

    project_root = find_project_root(skip_files=())

    # Initialize context - use test mode for logging, real DB mode for data if requested
    # We use a dummy resource first, then update it after DB is ready
    conn_str = os.getenv("MONGODB_CONNECTION_STRING", "mongodb://localhost:27017")
    db_name = os.getenv("MONGODB_DATABASE", "simstack_test")
    
    await context.initialize(
        console=False,
        is_test=True,
        resource="self",
        connection_string=conn_str if use_real_db else None,
        db_type=DBType.MONGODB if use_real_db else DBType.IN_MEMORY,
        db_name=db_name,
        workdir=working_dir,
        project_root=project_root
    )

    if use_real_db:
        await context.db.reset_database()
    else:
        # Patch ODMantic engine to work without sessions in test mode
        async def patched_save(instance, **kwargs):
            """Patched save method that doesn't use sessions"""
            # Use the collection directly without transactions
            collection = context.db.engine.get_collection(type(instance))

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
        context.db.engine.save = patched_save
        context.db.engine.save_all = patched_save_all
        context.db.engine.save_unchecked = patched_save

    # Initialize model and node tables for both real and mock databases
    dirs = ["src/simstack/models", "src/simstack/methods", "tests"]
    await make_model_table(context.db.engine, dirs=dirs, drops="src", clear=True, project_root=project_root)
    await make_node_table(context.db.engine, dirs=dirs, drops="src", clear=True, project_root=project_root)

    # Ensure a "test" resource exists in DB for tests
    from simstack.models.resource_definition import ResourceDefinition
    local_resource = ResourceDefinition(
        resource_str="test",
        hostname="localhost",
        workdir=working_dir,
        routes=[]
    )
    await context.db.engine.save(local_resource)

    # Now re-initialize with the "local" resource after it has been saved to DB
    from simstack.util.config_reader import ConfigReader
    from unittest.mock import MagicMock
    from simstack.core.resources import allowed_resources
    
    # Reset allowed_resources to allow second initialization
    allowed_resources.clear_resources()
    allowed_resources.add_resource("test")
    
    # Mock TomlReader to avoid file access
    mock_toml = MagicMock()
    mock_toml.use_db.return_value = True
    context.config = await ConfigReader.create("test", context.db, mock_toml, project_root=project_root, workdir=working_dir)

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
            context.path_manager = None
            context.config = None
            print("Test context cleaned up")
    except Exception as e:
        print(f"Warning: Error during context cleanup: {e}")


@pytest.fixture(scope="session")
def odmantic_engine(initialized_context):
    """
    Create an ODMantic engine for the entire test session.
    """
    return context.db.engine


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


