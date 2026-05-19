import pytest
import pytest_asyncio

from simstack.core.context import context
from simstack.core.definitions import DBType
from simstack.tables.model_table import make_model_table
from simstack.tables.node_table import make_node_table
from simstack.models.files import FileStack
from simstack.util.project_root_finder import find_project_root


@pytest_asyncio.fixture(autouse=True, scope="function")
async def initialized_context(tmp_path_factory, event_loop):
    # Use environment variable to control the database type for tests
    import os

    db_mode = os.getenv("SIMSTACK_TEST_USE_REAL_DB", "false").lower()
    use_real_db = db_mode == "true"

    if use_real_db and not _mongodb_available():
        raise RuntimeError(
            "SIMSTACK_TEST_USE_REAL_DB=true but MongoDB not available at localhost:27017"
        )

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
    await context.initialize(
        console=False,
        is_test=True,
        resource="local",
        connection_string="mongodb://localhost:27017" if use_real_db else None,
        db_type=DBType.MONGODB if use_real_db else DBType.IN_MEMORY,
        db_name="simstack_test",
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

    # Initialize model and node tables for both real and mock databases
    dirs = ["src/simstack/models", "src/simstack/methods", "tests"]
    await make_model_table(context.db.engine, dirs=dirs, drops="src")
    await make_node_table(context.db.engine, dirs=dirs, drops="src")

    if use_real_db:
        print("Test context initialized with real MongoDB database")
    else:
        print("Test context initialized with mock database (patched for mongomock)")

    if hasattr(context, "log_handler") and context.log_handler:
        root_logger = context.log_handler.root
        root_logger.setLevel("ERROR")

    # Provide the initialized context
    yield context

    # Cleanup after each test
    try:
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
def _mongodb_available():
    """Check if MongoDB is available on localhost:27017"""
    try:
        # Use a simple socket connection test instead of Motor client
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)  # 1 second timeout
        result = sock.connect_ex(("localhost", 27017))
        sock.close()
        return result == 0
    except Exception:
        return False


@pytest_asyncio.fixture(autouse=False, scope="function")
async def real_database_context():
    """
    Use the regular context but skip tests if real MongoDB is not available or if using mock database.
    For tests that require MongoDB features not supported by mongomock.

    Supports three modes:
    - SIMSTACK_TEST_USE_REAL_DB=false (default): Skip these tests
    - SIMSTACK_TEST_USE_REAL_DB=true: Run with real DB (already configured)
    """
    import os

    # Check database mode
    db_mode = os.getenv("SIMSTACK_TEST_USE_REAL_DB", "false").lower()

    if db_mode == "false":
        pytest.skip(
            "Test requires real MongoDB - set SIMSTACK_TEST_USE_REAL_DB=true to enable"
        )
    elif db_mode == "true":
        assert _mongodb_available(), "Real MongoDB not available at localhost:27017, but testing with real db was requested. Start using pixi run startmongo"
        # Use the regular context which should already be using real MongoDB
        yield context
    else:
        assert (
            False
        ), f"Invalid SIMSTACK_TEST_USE_REAL_DB value: {db_mode}. Use 'false', 'true'"
