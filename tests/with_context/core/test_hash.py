from simstack.core.context import context
from simstack.core.hash import hash_value, complex_hash_function
import hashlib

from simstack.models import FloatData

from pathlib import Path
from simstack.models.files import FileStack
import uuid
import shutil


def test_hash_value_with_string():
    result = hash_value("test_string")
    assert isinstance(result, str)
    assert len(result) == 64  # SHA-256 hash length


def test_hash_value_with_integer():
    result = hash_value(12345)
    assert isinstance(result, str)
    assert len(result) == 64
    assert result == hash_value(str(12345))  # Ensure consistency


def test_hash_value_with_float():
    result = hash_value(123.45)
    assert isinstance(result, str)
    assert len(result) == 64
    assert result == hash_value(str(123.45))  # Ensure consistency


def test_hash_value_with_empty_string():
    result = hash_value("")
    assert isinstance(result, str)
    assert len(result) == 64
    assert result == hashlib.sha256("".encode()).hexdigest()


def test_hash_value_with_boolean():
    result_true = hash_value(True)
    result_false = hash_value(False)
    assert isinstance(result_true, str)
    assert isinstance(result_false, str)
    assert len(result_true) == 64
    assert len(result_false) == 64
    assert result_true != result_false


def test_basic_hashing():
    float_data1 = FloatData(value=1.0)
    float_data2 = FloatData(value=1.0)
    assert complex_hash_function(float_data1) == complex_hash_function(float_data2)


def test_filestack_in_memory_hash():
    temp_path = Path(context.config.workdir)

    # Create unique test subdirectory to avoid conflicts
    test_subdir = temp_path / f"test_{uuid.uuid4().hex[:8]}"
    test_subdir.mkdir(parents=True, exist_ok=True)

    try:
        # First file with content "Hello World"
        file1_path = test_subdir / "test_file.txt"
        with open(file1_path, "w") as f:
            f.write("Hello World")

        filestack1 = FileStack.from_local_file(
            str(file1_path), in_memory=True, is_hashable=True, secure_source=True
        )
        hash2 = complex_hash_function(filestack1)
        assert hash2 == filestack1.hash, "Hash should match the FileStack's hash"

    finally:
        # Clean up the test directory
        shutil.rmtree(test_subdir, ignore_errors=True)


def test_filestack_hash_different_content_same_name():
    """
    Test that two FileStack objects with the same name and attributes
    but different content produce different hashes.
    """

    temp_path = Path(context.config.workdir)

    # Create unique test subdirectory to avoid conflicts
    test_subdir = temp_path / f"test_{uuid.uuid4().hex[:8]}"
    test_subdir.mkdir(parents=True, exist_ok=True)

    try:
        # First file with content "Hello World"
        file1_path = test_subdir / "test_file.txt"
        with open(file1_path, "w") as f:
            f.write("Hello World")

        # Second file with content "Goodbye World"
        file2_path = test_subdir / "test_file_copy.txt"
        with open(file2_path, "w") as f:
            f.write("Goodbye World")

        # Create FileStack objects with same name but different content
        filestack1 = FileStack.from_local_file(
            str(file1_path), in_memory=True, is_hashable=True, secure_source=True
        )

        filestack2 = FileStack.from_local_file(
            str(file2_path), in_memory=True, is_hashable=True, secure_source=True
        )

        # Rename second Filestack to have same name as first
        filestack2.name = filestack1.name

        # Ensure all other attributes are the same
        assert filestack1.name == filestack2.name
        assert filestack1.is_hashable == filestack2.is_hashable

        # But content should be different
        assert filestack1.content != filestack2.content

        # Hash both FileStack objects
        hash1 = complex_hash_function(filestack1)
        hash2 = complex_hash_function(filestack2)

        # Verify that hashes are different despite same name and attributes
        assert (
            hash1 != hash2
        ), f"Hashes should be different for different content, but got: {hash1} == {hash2}"

        # Verify hashes are consistent (calling multiple times gives the same result)
        hash1_repeat = complex_hash_function(filestack1)
        hash2_repeat = complex_hash_function(filestack2)

        assert hash1 == hash1_repeat, "Hash should be consistent for same object"
        assert hash2 == hash2_repeat, "Hash should be consistent for same object"

    finally:
        # Clean up the test directory
        shutil.rmtree(test_subdir, ignore_errors=True)


def test_filestack_hash_same_content_same_hash():
    """
    Test that two FileStack objects with identical content produce the same hash.
    """

    temp_path = Path(context.config.workdir)

    # Create unique test subdirectory to avoid conflicts
    test_subdir = temp_path / f"test_{uuid.uuid4().hex[:8]}"
    test_subdir.mkdir(parents=True, exist_ok=True)

    try:
        # Create two files with identical content
        content = "This is identical content for testing"

        file1_path = test_subdir / "file1.txt"
        file2_path = test_subdir / "file2.txt"

        with open(file1_path, "w") as f:
            f.write(content)

        with open(file2_path, "w") as f:
            f.write(content)

        # Create FileStack objects
        filestack1 = FileStack.from_local_file(
            str(file1_path), in_memory=True, is_hashable=True, secure_source=True
        )

        filestack2 = FileStack.from_local_file(
            str(file2_path), in_memory=False, is_hashable=True, secure_source=True
        )

        # Make names the same for fair comparison
        filestack2.name = filestack1.name

        # Hash both objects
        hash1 = complex_hash_function(filestack1)
        hash2 = complex_hash_function(filestack2)

        # Should produce same hash for identical content
        assert (
            hash1 == hash2
        ), f"Identical content should produce same hash: {hash1} vs {hash2}"

    finally:
        # Clean up the test directory
        shutil.rmtree(test_subdir, ignore_errors=True)
