import hashlib
from pathlib import Path
from typing import Union, BinaryIO

def hash_file(
    file_path: Union[str, Path], algorithm: str = "sha256", chunk_size: int = 8192
) -> str:
    """
    Calculate hash of a file on disk.

    Args:
        file_path: Path to the file
        algorithm: Hash algorithm (md5, sha1, sha256, etc.)
        chunk_size: Size of chunks to read

    Returns:
        str: Hexadecimal digest of the hash
    """
    hash_obj = hashlib.new(algorithm)

    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            hash_obj.update(chunk)

    return hash_obj.hexdigest()


def hash_file_object(
    file_obj: BinaryIO, algorithm: str = "sha256", chunk_size: int = 8192
) -> str:
    """
    Calculate hash of a file-like object.

    Args:
        file_obj: File-like object (must be in binary mode)
        algorithm: Hash algorithm (md5, sha1, sha256, etc.)
        chunk_size: Size of chunks to read

    Returns:
        str: Hexadecimal digest of the hash
    """
    hash_obj = hashlib.new(algorithm)

    # Store current position
    try:
        original_position = file_obj.tell()
    except (OSError, IOError, AttributeError):
        original_position = None

    # Seek to the beginning if possible
    try:
        file_obj.seek(0)
    except (OSError, IOError, AttributeError):
        pass

    # Calculate hash
    for chunk in iter(lambda: file_obj.read(chunk_size), b""):
        hash_obj.update(chunk)

    # Restore original position if possible
    if original_position is not None:
        try:
            file_obj.seek(original_position)
        except (OSError, IOError, AttributeError):
            pass

    return hash_obj.hexdigest()

