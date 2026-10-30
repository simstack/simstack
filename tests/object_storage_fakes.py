"""External byte storage for tests of workflow behavior, without network I/O."""

from datetime import datetime
import hashlib
from pathlib import Path
from uuid import uuid4

from simstack.models.files import FileStack
from simstack.models.file_instance import FileInstance

OBJECTS = {}


def location_for(payload, key=None):
    key = key or uuid4().hex
    OBJECTS[key] = bytes(payload)
    return FileInstance(
        path=key,
        resource="__s3__",
        location_type="s3",
        bucket="test-bucket",
        object_key=key,
        size_bytes=len(payload),
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        created_at=datetime.now(),
    )


def stored_file(*, name, content=None, in_memory=False, **metadata):
    """Replace old hand-built inline-content fixtures with S3 metadata fixtures."""
    payload = content or b""
    metadata.setdefault("size", len(payload))
    if metadata.get("is_hashable") and not metadata.get("hash"):
        metadata["hash"] = hashlib.sha256(payload).hexdigest()
    locations = metadata.pop("locations", [])
    return FileStack(
        name=name,
        in_memory=False,
        content=None,
        locations=[*locations, location_for(payload)],
        **metadata,
    )


class ObjectStorage:
    def publish(self, value, path):
        return location_for(Path(path).read_bytes()).model_dump()
