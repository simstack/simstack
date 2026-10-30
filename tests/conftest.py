import pytest
import asyncio


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def external_object_storage(monkeypatch):
    """Workflow tests keep running against an external bytes/metadata boundary.

    Transport/checksum tests exercise real HTTP separately; no production API or
    storage credentials are required to test execution, caching and file lists.
    """
    from pathlib import Path
    from datetime import datetime
    from tests.object_storage_fakes import OBJECTS, ObjectStorage
    from simstack.models.files import FileStack
    from simstack.models.file_instance import FileInstance
    from simstack.util.object_transfer import ObjectFileClient
    from simstack.util.file_transfer_client import path_for_file_instance

    monkeypatch.setattr(ObjectFileClient, "from_context", lambda **kwargs: ObjectStorage())
    original = FileStack._get_via_server_transfer
    def materialize(self, remote, resource, directory):
        if remote.location_type != "s3" or remote.object_key not in OBJECTS:
            return original(self, remote, resource, directory)
        target = Path(directory) / self.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        target.write_bytes(OBJECTS[remote.object_key])
        from simstack.core.context import context
        try:
            path = path_for_file_instance(target, context.config.workdir)
        except (RuntimeError, AttributeError):
            path = str(target)
        self.locations.append(FileInstance(path=path, resource=resource, created_at=datetime.now(),
            is_cached=True, size_bytes=remote.size_bytes, checksum_sha256=remote.checksum_sha256))
        return target
    monkeypatch.setattr(FileStack, "_get_via_server_transfer", materialize)
    yield
    OBJECTS.clear()
