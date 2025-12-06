import os
import shutil
import zlib
from datetime import datetime
from pathlib import Path
import pytest
from unittest.mock import patch
from typing import Type, TypeVar
import tempfile

from simstack.models.files import FileStack
from simstack.models.file_instance import FileInstance

# Define a TypeVar for classes
T = TypeVar("T", bound=Type)

#TODO why do filetests not use the context fixture from conftest?

@pytest.fixture
def setup_test_env(initialized_context, monkeypatch):
    """Set up test environment with initialized context"""
    # Create test directory structure
    test_workdir = tempfile.mkdtemp(prefix="simstack_test_")

    # Temporarily override the workdir in context
    original_workdir = initialized_context.config.workdir
    initialized_context.config._workdir = test_workdir

    try:
        os.makedirs(test_workdir, exist_ok=True)
        yield initialized_context
    finally:
        # Clean up temporary directory
        if os.path.exists(test_workdir):
            shutil.rmtree(test_workdir, ignore_errors=True)

        # Restore original workdir
        initialized_context.config._workdir = original_workdir


@pytest.fixture
def test_file(tmp_path):
    """Create a temporary test file"""
    file_path = tmp_path / "test_file.txt"
    file_path.write_text("This is a test file content")
    return file_path


# FileInstance Tests


@pytest.fixture
def file_instance():
    """Create a basic FileInstance for testing"""
    return FileInstance(
        path="test/path/file.txt",
        resource="test_resource",
        created_at=datetime(2023, 1, 1, 12, 0, 0),
    )


def test_file_instance_creation():
    """Test basic FileInstance creation"""
    instance = FileInstance(
        path="test/path/file.txt",
        resource="local",
        created_at=datetime(2023, 1, 1, 12, 0, 0),
    )

    assert instance.path == "test/path/file.txt"
    assert instance.resource == "local"
    assert instance.created_at == datetime(2023, 1, 1, 12, 0, 0)


def test_from_local_file(test_file, setup_test_env):
    """Test creating FileInstance from a local file"""
    context = setup_test_env
    temp_dir = tempfile.mkdtemp(prefix="simstack_test_file_")

    try:
        # Create test file in temporary directory
        temp_path = Path(context.config.workdir) / "test_file.txt"
        with open(temp_path, "w") as f:
            f.write("test content")

        # Patch the context import that happens inside the from_local_file method
        with patch("simstack.core.context.context", context):
            from simstack.models.file_instance import FileInstance
            from bson import ObjectId

            file_instance = FileInstance.from_local_file(
                path=temp_path, file_stack_id=ObjectId(), make_copy=False
            )

            assert file_instance.resource == context.config.resource

    finally:
        # Clean up temporary files
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def test_from_local_file_no_copy(test_file, setup_test_env):
    """Test creating FileInstance without making a copy"""
    context = setup_test_env
    file_stack_id = "test_stack_id"

    temp_dir = tempfile.mkdtemp(prefix="simstack_test_file_")

    try:
        # Setup: make sure the test file is within the workdir
        rel_path = Path("rel/path")
        full_path = Path(temp_dir) / rel_path / test_file.name
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        shutil.copy(test_file, full_path)

        # Mock the relative_to function
        with patch.object(Path, "relative_to", return_value=rel_path):
            instance = FileInstance.from_local_file(
                path=full_path, file_stack_id=file_stack_id, make_copy=False
            )

        assert rel_path.name in instance.path
        assert instance.resource == context.config.resource

    finally:
        # Clean up temporary files
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def test_from_local_file_error(tmp_path):
    """Test error handling in from_local_file"""
    non_existent_file = tmp_path / "non_existent.txt"

    with pytest.raises(ValueError):
        FileInstance.from_local_file(path=non_existent_file, file_stack_id="any_id")


# FileStack Tests


@pytest.fixture
def file_stack():
    """Create a basic FileStack for testing"""
    return FileStack(
        name="test_file.txt", size=100, is_hashable=True, in_memory=False, locations=[]
    )


def test_file_stack_creation():
    """Test basic FileStack creation"""
    stack = FileStack(name="test_file.txt", size=100, is_hashable=True, in_memory=False)

    assert stack.name == "test_file.txt"
    assert stack.size == 100
    assert stack.is_hashable is True
    assert stack.in_memory is False
    assert stack.locations == []


@pytest.mark.asyncio
async def test_custom_model_dump(file_stack):
    """Test the custom_model_dump method"""
    # Add content to ensure it's excluded
    file_stack.content = b"test content"

    result = await file_stack.custom_model_dump()

    assert "content" not in result
    assert "name" in result
    assert "size" in result


def test_ui_base_schema():
    """Test the ui_base_schema class method"""
    schema = FileStack.ui_base_schema()

    assert schema["ui:field"] == "FileField"
    assert "model" in schema["ui:options"]
    assert schema["ui:options"]["model"] == "simstack.models.files.FileStack"


def test_from_local_file_in_memory(test_file, setup_test_env):
    """Test creating FileStack from a local file with in-memory storage"""
    temp_dir = tempfile.mkdtemp(prefix="simstack_test_file_")

    try:
        # Copy test file to temporary location
        temp_file = Path(temp_dir) / test_file.name
        shutil.copy(test_file, temp_file)

        with patch("simstack.models.files.hash_file", return_value="test_hash"):
            stack = FileStack.from_local_file(
                path=temp_file, is_hashable=True, in_memory=True
            )

        assert stack.name == temp_file.name
        assert stack.is_hashable is True
        assert stack.in_memory is True
        assert stack.content is not None  # Compressed content should exist

    finally:
        # Clean up temporary files
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def test_from_local_file_not_in_memory(test_file, setup_test_env):
    """Test creating FileStack from a local file without in-memory storage"""
    temp_dir = tempfile.mkdtemp(prefix="simstack_test_file_")

    try:
        # Copy test file to temporary location
        temp_file = Path(temp_dir) / test_file.name
        shutil.copy(test_file, temp_file)

        with patch("simstack.models.files.hash_file", return_value="test_hash"):
            stack = FileStack.from_local_file(
                path=temp_file, is_hashable=True, in_memory=False
            )

        assert stack.name == temp_file.name
        assert stack.is_hashable is True
        assert stack.in_memory is False
        assert stack.content is None  # No content stored
        assert len(stack.locations) == 1  # Should have one location

    finally:
        # Clean up temporary files
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def test_from_local_file_nonexistent():
    """Test error when file doesn't exist"""
    with pytest.raises(FileNotFoundError):
        FileStack.from_local_file(path="nonexistent_file.txt")


def test_from_local_file_directory(tmp_path):
    """Test error when path is a directory"""
    with pytest.raises(ValueError):
        FileStack.from_local_file(path=tmp_path)


def test_append_location(file_stack, file_instance):
    """Test appending a FileInstance to the stack"""
    file_stack.append(file_instance)

    assert len(file_stack.locations) == 1
    assert file_stack.locations[0] == file_instance


def test_get_in_memory(file_stack, setup_test_env):
    """Test getting file content when in memory"""
    context = setup_test_env
    # Setup
    file_stack.name = "test.txt"
    file_stack.in_memory = True
    original_content = b"Test content"
    file_stack.content = zlib.compress(original_content)

    # Mock context config
    with patch("getpass.getuser", return_value="testuser"):
        # Create the necessary directory structure
        user_dir = Path(context.config.workdir) / "testuser" / str(file_stack.id)
        os.makedirs(user_dir, exist_ok=True)

        # Test
        result_path = file_stack.get(context.config.resource, user_dir / file_stack.name)

        # Verify
        assert os.path.exists(result_path)
        with open(result_path, "rb") as f:
            assert f.read() == original_content


def test_get_same_resource(file_stack, file_instance, setup_test_env):
    """Test getting file when not in memory but on same resource"""
    context = setup_test_env

    # Setup
    file_stack.in_memory = False
    file_instance.resource = context.config.resource
    file_stack.locations.append(file_instance)

    # Test

    # Mock context config
    with patch("getpass.getuser", return_value="testuser"):
        # Create the necessary directory structure
        user_dir = Path(context.config.workdir) / "testuser" / str(file_stack.id)
        os.makedirs(user_dir, exist_ok=True)


        result_path = file_stack.get(context.config.resource, user_dir / file_stack.name)

        # Verify
        assert result_path == Path(file_instance.path)


def test_get_no_suitable_instance(file_stack, setup_test_env):
    """Test error when no suitable file instance available"""
    # Setup
    context = setup_test_env
    file_stack.in_memory = False
    file_stack.locations = []  # No locations

    # Mock context config
    with patch("getpass.getuser", return_value="testuser"):
        # Create the necessary directory structure
        user_dir = Path(context.config.workdir) / "testuser" / str(file_stack.id)
        os.makedirs(user_dir, exist_ok=True)

    # Test
        with pytest.raises(ValueError):
            file_stack.get(context.config.resource, user_dir / file_stack.name)


def test_str_representation(file_stack):
    """Test string representation"""
    file_stack.name = "test.txt"
    file_stack.size = 100

    result = file_stack.str()

    assert "FileStack" in result
    assert "test.txt" in result
    assert "100" in result
