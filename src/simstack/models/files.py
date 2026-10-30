from __future__ import annotations

import logging
import os
import zlib
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union, Dict, Any

from odmantic import Model, Field, ObjectId, Reference
from pydantic import model_validator

from simstack.models import simstack_model
from simstack.models.file_instance import FileInstance
from simstack.models.parameters import Resource
from simstack.util.file_transfer_client import (
    FileTransferClient,
    first_available_remote_location,
    path_for_file_instance,
    transfer_resource_name,
)
from simstack.util.file_hashing import hash_file, hash_string

logger = logging.getLogger(__name__)

# MongoDB has a 16MB document size limit
MONGODB_MAX_DOCUMENT_SIZE = 16 * 1024 * 1024  # 16 MB in bytes

# TODO: its a mystery to me why we should have files with no name or unknown size, both values are actually used in __str__

@simstack_model
class FileStack(Model):
    name: Optional[str] = Field(description="Name of the file", default=None)
    size: Optional[int] = Field(description="Size of the file in bytes", default=None)
    is_hashable: bool = Field(
        default=False, description="Whether the file stack is hashable"
    )
    hash: Optional[str] = Field(
        default=None, description="Hash of the file stack content", index=True
    )
    in_memory: bool = Field(
        default=False, description="Whether the file stack is in memory"
    )
    content: Optional[bytes] = Field(
        description="Compressed file content", default=None
    )

    locations: List[FileInstance] = Field(
        default_factory=list, description="List of file locations"
    )

    @model_validator(mode="after")
    def require_external_file_content(self):
        if self.content is not None or self.in_memory:
            raise ValueError("FileStack bytes must be stored in S3; use from_string or from_local_file")
        return self

    def __str__(self) -> str:
        return f"FileStack(name={self.name}, size={self.size}, is_hashable={self.is_hashable}, in_memory={self.in_memory}, locations={self.locations})"

    def __repr__(self) -> str:
        return f"FileStack(name={self.name}, size={self.size}, is_hashable={self.is_hashable}, in_memory={self.in_memory}, locations={self.locations})"

    async def custom_model_dump(self, **kwargs: Any) -> Dict[str, Any]:
        dumped_data: Dict[str, Any] = self.model_dump()
        del dumped_data["content"]  # Exclude content from the dumped data
        return dumped_data

    @classmethod
    def ui_base_schema(cls, **kwargs: Any) -> Dict[str, Any]:
        # TODO get the model programatically
        return {
            "ui:field": "FileField",
            "ui:options": {
                "model": "simstack.models.files.FileStack",
            },
        }

    @classmethod
    def from_string(cls, data_string: str, file_name: str) -> FileStack:
        import tempfile

        # A pure constructor: import-time/default values need no initialized
        # context or credentials. File bytes are staged on disk until saved.
        try:
            from simstack.core.context import context
            parent = Path(context.config.workdir) / "file-staging"
            parent.mkdir(parents=True, exist_ok=True)
        except (RuntimeError, AttributeError):
            parent = None
        directory = tempfile.mkdtemp(prefix="simstack-file-", dir=parent)
        import atexit, shutil
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / (Path(file_name).name or "file")
        path.write_text(data_string, encoding="utf-8")
        value = cls.from_local_file(path, is_hashable=True, secure_source=True)
        value.name = file_name
        object.__setattr__(value, "_staging_directory", directory)
        return value

    @classmethod
    def from_local_file(
        cls,
        path: Union[Path, str],
        is_hashable: bool = True,
        in_memory: bool = True,
        secure_source: bool = False,
        task_id: str = "",
    ) -> FileStack:
        """
        Creates a FileStack object from a local file path.

        :param task_id: task_id of the task that created the file stack, used for logging and tracking
        :type task_id: str
        :param secure_source: specifies if the source is secure (already in a directory generated within Simstack II)
        :type secure_source: bool
        :param path: The path to the local file. Can be provided as a string or Path object.
        :type path: Union[Path, str]
        :param is_hashable: A flag indicating whether the file hash needs to be calculated.
        :type is_hashable: bool
        :param in_memory: Retained for workflow compatibility; bytes are always stored in S3.
        :type in_memory: bool

        :return: A FileStack object containing FileInstances for the file.
        :rtype: FileStack
        """
        # TODO make a second version where a unique directory is already there
        source_path = path if isinstance(path, Path) else Path(path)

        # Check if the source exists
        if not source_path.exists():
            logger.error(f"Source file not found: {source_path}")
            raise FileNotFoundError(f"Source file not found: {source_path}")

        # Check if it's a file (not a directory)
        if not source_path.is_file():
            logger.error(f"Source file {path} is not a file")
            raise ValueError(f"Source file is not a file: {path}")

        # Check read permission using os.access
        if not os.access(path, os.R_OK):
            logger.error(f"No read permission for file: {path}")
            raise ValueError(f"No read permission for file: {path}")

        file_stack = cls(
            name=source_path.name,
            in_memory=False,
            content=None,
            is_hashable=is_hashable,
            size=source_path.stat().st_size,
            hash=hash_file(source_path) if is_hashable else None,
        )
        try:
            from simstack.core.context import context
            config = context.config
        except (RuntimeError, AttributeError):
            config = None
        if config is not None and getattr(config, "workdir", None) is not None:
            location = FileInstance.from_local_file(
                path=source_path, file_stack_id=file_stack.id,
                make_copy=not secure_source)
        else:
            location = FileInstance(
                path=str(source_path.resolve()), resource="__pending__",
                created_at=datetime.now(), size_bytes=file_stack.size,
                checksum_sha256=hash_file(source_path))
        file_stack.locations.append(location)
        return file_stack

    def ensure_uploaded(self):
        """Publish staged bytes before persistence; constructors remain local."""
        if any(location.location_type == "s3" and location.status == "available"
               for location in self.locations):
            return
        from simstack.core.context import context
        from simstack.util.object_transfer import ObjectFileClient
        resource = Resource(value=transfer_resource_name(context.config.resource))
        for location in self.locations:
            if location.location_type != "local_path":
                continue
            path = Path(location.path)
            if not path.is_absolute():
                path = Path(context.config.workdir) / path
            if not path.is_file():
                continue
            if transfer_resource_name(location.resource) == "__pending__":
                location.resource = resource
            # API metadata + direct S3 transfer; no content enters a DB write.
            uploaded = ObjectFileClient.from_context(required=True).publish(self, path)
            self.locations.append(FileInstance.model_validate(uploaded))
            staged = getattr(self, "_staging_directory", None)
            if staged:
                import shutil
                shutil.rmtree(staged, ignore_errors=True)
                self.locations = [value for value in self.locations if value.location_type == "s3"]
                object.__setattr__(self, "_staging_directory", None)
            return
        raise FileNotFoundError(f"No local source exists to publish FileStack {self.id}")

    def complex_hash(self) -> str:
        if self.is_hashable:
            if self.hash:
                return self.hash
            else:
                raise ValueError("FileStack is hashable but hash is not set.")
            # elif self.in_memory and self.content:
            #    # If the content is in memory, hash the compressed content
            #    return complex_hash_function(zlib.decompress(self.content))
            # else:
            #    temp_dir = Path(tempfile.mkdtemp())
            #    local_file = self.get(None, local_dir=temp_dir)
            #    return complex_hash_function(local_file.read_bytes())
        else:
            logger.warning(
                f"FileStack {self.id} is not hashable, returning unique hash."
            )
            return str(ObjectId())

    def append(self, file_instance: FileInstance) -> None:
        """
        Appends a FileInstance to the file stack.

        :param file_instance: The FileInstance to append.
        :type file_instance: FileInstance
        """
        self.locations.append(file_instance)

    def get(self, local_dir: Path | None = None) -> Path:
        """
        Copies the file stack to a local directory. This is the version to be used in applications

        :param local_dir: The local directory to copy the file stack to.
        :type local_dir: Path
        """
        pending = any(transfer_resource_name(value.resource) == "__pending__"
            for value in self.locations)
        if pending:
            return self.get_raw(Resource(value="__pending__"), local_dir)
        from simstack.core.context import context
        return self.get_raw(context.config.resource, local_dir)

    def get_raw(self, local_resource: Resource, local_dir: Path | None = None) -> Path:
        """
        Copies the file stack to a local directory, assumes no context.

        :param local_resource: the local resource to copy the file stack to. Defaults to the current resource.
        :param local_dir: The local directory to copy the file stack to.
        :type local_dir: Path
        """

        # select the best instance
        # first search for an instance with "in_memory" set to True

        if local_dir is None:
            local_dir = Path.cwd()

        pending = next((value for value in self.locations
            if transfer_resource_name(value.resource) == "__pending__"
            and Path(value.path).is_file()), None)
        if pending is not None:
            local_dir.mkdir(parents=True, exist_ok=True)
            target = local_dir / (Path(self.name).name if self.name else Path(pending.path).name)
            if target.exists():
                raise FileExistsError(f"File {target} already exists.")
            import shutil
            shutil.copyfile(pending.path, target)
            return target

        local_resource_name = transfer_resource_name(local_resource)
        local_transfer_resource = Resource(value=local_resource_name)

        # If in-memory instance not found or decompression failed, try finding instance on same resource
        for instance in self.locations:
            if (instance.location_type != "local_path"
                    or transfer_resource_name(instance.resource) != local_resource_name
                    or instance.status != "available"):
                continue
            from simstack.core.context import context
            path = Path(instance.path)
            if not path.is_absolute():
                path = Path(context.config.workdir) / path
            if path.is_file():
                instance.last_accessed_at = datetime.now()
                return path

        remote_instance = next((location for location in self.locations
            if location.location_type == "s3" and location.status == "available"), None)
        if remote_instance is None:
            remote_instance = first_available_remote_location(self.locations, local_transfer_resource)
        if remote_instance is None:
            logger.error("No suitable file instance found for copying.")
            raise ValueError(
                f"FileStack {self.id} {self.name} is unavailable: no accessible file instance for resource {local_transfer_resource}."
            )

        local_dir.mkdir(parents=True, exist_ok=True)
        return self._get_via_server_transfer(
            remote_instance, local_transfer_resource, local_dir
        )

    def _get_via_server_transfer(
        self,
        remote_instance: FileInstance,
        local_resource: Resource,
        local_dir: Path,
    ) -> Path:
        from simstack.core.context import context

        client = FileTransferClient.from_context(required=True)
        assert client is not None

        transfer = client.create_transfer(
            file_stack_id=str(self.id),
            source_file_instance_id=getattr(remote_instance, "id", None),
            source_resource_name=transfer_resource_name(remote_instance.resource),
            target_resource_name=transfer_resource_name(local_resource),
        )
        transfer_id = str(transfer["transfer_id"])
        logger.info(
            "Created FileStack transfer %s for file_stack=%s from resource=%s to resource=%s",
            transfer_id,
            self.id,
            transfer_resource_name(remote_instance.resource),
            transfer_resource_name(local_resource),
        )

        try:
            client.wait_until_uploaded(transfer_id)
        except Exception as exc:
            try:
                client.fail_transfer(
                    transfer_id,
                    error_message=str(exc),
                    error_code="SOURCE_RUNNER_UNAVAILABLE",
                )
            except Exception:
                logger.warning(
                    "Failed to report failed FileStack transfer %s", transfer_id
                )
            raise
        target_path = local_dir / (self.name or Path(str(remote_instance.path)).name)
        if target_path.exists():
            raise FileExistsError(f"File {target_path} already exists.")
        try:
            downloaded = client.download_file(transfer_id, target_path)
        except Exception as exc:
            try:
                client.fail_transfer(transfer_id, error_message=str(exc), error_code="TARGET_DOWNLOAD_FAILED")
            except Exception:
                logger.warning("Failed to report download failure for transfer %s", transfer_id)
            raise
        instance_path = path_for_file_instance(downloaded.path, context.config.workdir)
        completed = client.complete_transfer(
            transfer_id=transfer_id,
            target_resource_name=transfer_resource_name(local_resource),
            target_path=instance_path,
            size_bytes=downloaded.size_bytes,
            checksum_sha256=downloaded.checksum_sha256,
        )

        file_instance_id = completed.get("file_instance_id")
        existing = next(
            (
                location
                for location in self.locations
                if getattr(location, "id", None) == file_instance_id
            ),
            None,
        )
        if existing is None:
            self.locations.append(
                FileInstance(
                    id=str(file_instance_id) if file_instance_id else None,
                    path=instance_path,
                    resource=local_resource,
                    created_at=datetime.now(),
                    size_bytes=downloaded.size_bytes,
                    checksum_sha256=downloaded.checksum_sha256,
                    location_type="local_path",
                    is_cached=True,
                    status="available",
                )
            )

        return downloaded.path

    def str(self) -> str:
        return f"FileStack(name={self.name}, size={self.size}, is_hashable={self.is_hashable}, in_memory={self.in_memory}, locations={self.locations})"


class FileGetterArgs(Model):
    file_stack: FileStack = Reference()
    local_resource: Resource
    local_dir: Path


async def main() -> None:
    from simstack.core.context import context

    await context.initialize()

    # write a file test.txt
    with open("test.txt", "w") as f:
        f.write("Hello World")
    file_stack = FileStack.from_local_file("test.txt", is_hashable=True, in_memory=True)
    print(file_stack)
    await context.db.save(file_stack)
    local_dir = Path(context.config.workdir) / "samira" / str(file_stack.id)
    retrieved = file_stack.get(local_dir=local_dir)
    print("Retrieved file path:", retrieved)


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())
