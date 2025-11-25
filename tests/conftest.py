import sys
from pathlib import Path

import pytest
import pytest_asyncio
import asyncio
from simstack.core.context import context
from simstack.core.model_table import make_model_table
from simstack.core.node_table import make_node_table
from simstack.models.files import FileStack
from simstack.util.project_root_finder import find_project_root

import threading
import queue


@pytest_asyncio.fixture(autouse=True, scope="session")
async def initialized_context(tmp_path_factory):
    # Use environment variable to control the database type for tests
    import os

    db_mode = os.getenv("SIMSTACK_TEST_USE_REAL_DB", "false").lower()
    use_real_db = db_mode == "true"

    if use_real_db and not _mongodb_available():
        raise RuntimeError(
            "SIMSTACK_TEST_USE_REAL_DB=true but MongoDB not available at localhost:27017"
        )

    working_dir = tmp_path_factory.mktemp("simstack_test")
    # Initialize context - use test mode for logging, real DB mode for data if requested
    context.initialize(
        console=False,
        is_test=True,
        connection_string="mongodb://localhost:27017" if use_real_db else None,
        db_name="simstack_test" if use_real_db else None,
        workdir=working_dir,
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

        # Apply patches only for mock database
        context.db.engine.save = patched_save
        context.db.engine.save_all = patched_save_all

    # Initialize model and node tables for both real and mock databases
    await make_model_table(context.db.engine)
    await make_node_table(context.db.engine)

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
            # Close main database connection
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
    except Exception as e:
        print(f"Warning: Error during context cleanup: {e}")


@pytest.fixture(scope="session")
def event_loop():
    # This event_loop is required, because the default pytest-asyncio event loop is function scoped
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


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


@pytest.fixture(scope="session")
def test_runner():
    """
    Fixture to run and manage the test runner process.
    """
    import subprocess
    import platform
    import time

    # allowed_resources.add_resource("test_resource")
    root = Path(find_project_root())
    command = root / "src" / "simstack" / "core" / "runner.py"

    print("environment_start", context.config.environment_start)

    # Cross-platform command chaining
    system = platform.system().lower()
    env_start = (
        context.config.environment_start.strip()
        if context.config.environment_start
        else ""
    )

    if system == "windows":
        if env_start:
            command_string = f'cmd /c "{env_start} && {sys.executable} {command} --resource tests --db-name samira_test"'
        else:
            command_string = f'cmd /c "{sys.executable} {command} --resource tests --db-name samira_test"'
    else:
        if env_start:
            command_string = f"{env_start} && {sys.executable} {command} --resource tests --db-name samira_test"
        else:
            command_string = (
                f"{sys.executable} {command} --resource tests --db-name samira_test"
            )

    print(f"Starting subprocess with command: {command_string}")

    # Start the process with non-blocking pipes
    process = subprocess.Popen(
        command_string,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0,
    )  # Unbuffered

    # Queues to store output
    stdout_queue = queue.Queue()
    stderr_queue = queue.Queue()

    def read_stdout():
        try:
            for line in iter(process.stdout.readline, ""):
                if line:
                    line = line.strip()
                    stdout_queue.put(line)
                    print(f"[SUBPROCESS STDOUT]: {line}")
        except Exception as e:
            print(f"Error reading stdout: {e}")
        finally:
            if process.stdout:
                process.stdout.close()

    def read_stderr():
        try:
            for line in iter(process.stderr.readline, ""):
                if line:
                    line = line.strip()
                    stderr_queue.put(line)
                    print(f"[SUBPROCESS STDERR]: {line}")
        except Exception as e:
            print(f"Error reading stderr: {e}")
        finally:
            if process.stderr:
                process.stderr.close()

    # Start threads to read output immediately
    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    # Add queues to process object so tests can access them
    process.stdout_queue = stdout_queue
    process.stderr_queue = stderr_queue

    # Give the process a moment to start
    time.sleep(1)

    # Check if process started successfully
    if process.poll() is not None:
        print(f"Process exited early with code: {process.returncode}")
        # Try to get any error output
        time.sleep(0.5)  # Give threads time to read final output
        while not stderr_queue.empty():
            print(f"[SUBPROCESS STDERR]: {stderr_queue.get()}")
        while not stdout_queue.empty():
            print(f"[SUBPROCESS STDOUT]: {stdout_queue.get()}")
    else:
        print("Process started successfully")

    yield process

    # Cleanup: terminate the process
    print("Cleaning up subprocess...")
    if process.poll() is None:  # Process is still running
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("Process didn't terminate gracefully, killing...")
            process.kill()
            process.wait()

    # Wait for threads to finish
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)

    print("Subprocess cleanup complete")
    # allowed_resources.remove_resource("test_resource")


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
