"""Local inputs published by the real scientific/object API test fixture."""
from pathlib import Path
from uuid import uuid4

from simstack.core.context import context
from simstack.models.files import FileStack


def staged_file(*, name, content=None, size=None, in_memory=False, **metadata):
    payload = bytes(content) if content is not None else b"\x00" * (size or 0)
    directory = Path(context.config.workdir) / "fixture-files" / uuid4().hex
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(payload)
    value = FileStack.from_local_file(path, secure_source=True,
        is_hashable=metadata.pop("is_hashable", False))
    for key, item in metadata.items():
        setattr(value, key, item)
    return value
