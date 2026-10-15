from __future__ import annotations

import builtins
import json
import logging
import os
import re
import tempfile
import zlib
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional, Union, Dict, Any

from simstack.models.document import Field, Model, ObjectId, encode_document_value

from simstack.models import simstack_model
from simstack.models.file_instance import FileInstance
from simstack.models.parameters import Resource
from simstack.runner_api.file_transfer import (
    ExecutionFileTransferClient,
    FileTransferError,
    first_available_remote_location,
    path_for_file_instance,
    resolve_instance_path,
    transfer_resource_name,
)
from simstack.runner_protocol import FileTransferStatus
from simstack.util.file_hashing import hash_file, hash_string

logger = logging.getLogger(__name__)


def _compressed_file_content(path: Path, inline_limit: int | None) -> bytes | None:
    """Compress incrementally, choosing file-backed storage before memory grows."""
    compressor = zlib.compressobj()
    compressed = bytearray()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            compressed.extend(compressor.compress(chunk))
            if inline_limit is not None and len(compressed) > inline_limit:
                return None
    compressed.extend(compressor.flush())
    if inline_limit is not None and len(compressed) > inline_limit:
        return None
    return bytes(compressed)


def _inline_wire_size(file_stack: "FileStack") -> int:
    """Measure the complete portable representation, including real metadata."""
    document = encode_document_value(file_stack.model_dump_doc())
    return len(json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode())


def _materialize_compressed_file(content: bytes, target_path: Path) -> None:
    """Stream decompression to an isolated file and publish without overwriting."""
    decoder = zlib.decompressobj()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=target_path.parent, delete=False) as output:
            temp_path = Path(output.name)
            for offset in range(0, len(content), 1024 * 1024):
                pending = content[offset:offset + 1024 * 1024]
                while pending:
                    output.write(decoder.decompress(pending, 1024 * 1024))
                    pending = decoder.unconsumed_tail
            output.write(decoder.flush())
        if not decoder.eof or decoder.unused_data:
            raise ValueError("FileStack has incomplete or trailing compressed content")
        os.link(temp_path, target_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _strict_file_name(value: str | None) -> str:
    if not isinstance(value, str):
        raise ValueError("FileStack name is required")
    file_name = value
    if (
        file_name in {"", ".", ".."}
        or "\x00" in file_name
        or PurePosixPath(file_name).name != file_name
        or PureWindowsPath(file_name).name != file_name
        or PurePosixPath(file_name).is_absolute()
        or PureWindowsPath(file_name).is_absolute()
    ):
        raise ValueError("FileStack name must be a strict basename")
    return file_name


def _runner_inline_document_limit() -> int | None:
    from simstack.core.file_io import current_file_io
    from simstack.core.context import context

    provider = current_file_io()
    if provider is not None:
        return provider.max_inline_document_bytes
    if not context.initialized or not context.runner_mode:
        return None
    limit = getattr(context.config, "max_inline_document_bytes", None)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise RuntimeError(
            "API runner bootstrap has no valid max_inline_document_bytes capability"
        )
    request_limit = getattr(context.config, "max_runner_payload_bytes", None)
    if request_limit is None:
        return limit
    if isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit <= 0:
        raise RuntimeError("API runner bootstrap has no valid max_runner_payload_bytes policy")
    return min(limit, request_limit)

# TODO: its a mystery to me why we should have files with no name or unknown size, both values are actually used in __str__

@simstack_model
class FileStack(Model):
    name: Optional[builtins.str] = Field(description="Name of the file", default=None)
    size: Optional[int] = Field(description="Size of the file in bytes", default=None)
    deleted_at: Optional[datetime] = Field(
        default=None,
        description="When the file contents were deleted while retaining this identity for historical references",
    )
    is_hashable: bool = Field(
        default=False, description="Whether the file stack is hashable"
    )
    hash: Optional[builtins.str] = Field(
        default=None, description="Hash of the file stack content"
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
        return {
            "ui:field": "FileField",
            "ui:options": {
                "model": f"{cls.__module__}.{cls.__name__}",
            },
        }

    @classmethod
    def from_string(cls, data_string: str, file_name: str) -> FileStack:
        file_name = _strict_file_name(file_name)
        content = zlib.compress(data_string.encode("utf-8"))
        file_hash = hash_string(data_string)
        size = len(data_string.encode("utf-8"))

        inline_limit = _runner_inline_document_limit()
        file_stack = cls(
            name=file_name,
            in_memory=True,
            content=content,
            is_hashable=True,
            size=size,
            hash=file_hash,
        )
        if inline_limit is not None and _inline_wire_size(file_stack) > inline_limit:
            from simstack.core.file_io import current_file_io
            from simstack.core.context import context
            from simstack.core.runner_execution import current_runner_claim
            from simstack.runner_runtime.config import runner_task_workdir

            provider = current_file_io()
            task_dir = provider.staging_dir if provider is not None else runner_task_workdir(
                Path(context.config.workdir), current_runner_claim().task_id
            )
            task_dir.mkdir(parents=True, exist_ok=True)
            source_dir = Path(tempfile.mkdtemp(prefix="file-", dir=task_dir))
            source = source_dir / file_name
            source.write_bytes(data_string.encode("utf-8"))
            return cls.from_local_file(source, in_memory=False, secure_source=True)
        return file_stack

    @classmethod
    def from_local_file(
        cls,
        path: Union[Path, str],
        is_hashable: bool = True,
        in_memory: bool = True,
        secure_source: bool = False,
        task_id: str = "",
        max_inline_document_bytes: int | None = None,
        workdir: Path | str | None = None,
        resource: Resource | None = None,
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
        :param in_memory: Whether to store the compressed file content in memory. Defaults to True.
        :type in_memory: bool
        :param max_inline_document_bytes: Storage capacity advertised by the owner
            of persistence; larger compressed files use file-backed storage.

        :return: A FileStack object containing FileInstances for the file.
        :rtype: FileStack
        """
        # TODO make a second version where a unique directory is already there
        from simstack.core.file_io import current_file_io

        provider = current_file_io()
        if provider is not None and workdir is None and resource is None:
            workdir, resource = provider.workdir, provider.resource
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

        content = None
        file_hash = hash_file(source_path) if is_hashable else None
        name = source_path.name
        size = source_path.stat().st_size if source_path.exists() else None

        if max_inline_document_bytes is not None and (
            isinstance(max_inline_document_bytes, bool)
            or not isinstance(max_inline_document_bytes, int)
            or max_inline_document_bytes <= 0
        ):
            raise ValueError("max_inline_document_bytes must be a positive integer")
        if in_memory:
            inline_limit = _runner_inline_document_limit()
            if max_inline_document_bytes is not None:
                inline_limit = (
                    max_inline_document_bytes
                    if inline_limit is None
                    else min(inline_limit, max_inline_document_bytes)
                )
            content = _compressed_file_content(source_path, inline_limit)
            if content is None:
                logger.info(
                    "Compressed FileStack %s exceeds storage capacity; "
                    "using the file-transfer path",
                    source_path,
                )
                in_memory = False

        file_stack = cls(
            name=name,
            in_memory=in_memory,
            content=content,
            is_hashable=is_hashable,
            size=size,
            hash=file_hash,
        )
        if (
            in_memory
            and inline_limit is not None
            and _inline_wire_size(file_stack) > inline_limit
        ):
            in_memory = False
            file_stack.in_memory = False
            file_stack.content = None

        if not in_memory:
            location = FileInstance.from_local_file(
                path=path,
                file_stack_id=file_stack.id,
                make_copy=not secure_source and not in_memory,
                workdir=workdir,
                resource=resource,
            )
            file_stack.locations.append(location)

        return file_stack

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
        if self.deleted_at is not None:
            raise ValueError("FileStack content has been deleted")

        from simstack.core.context import context
        from simstack.core.file_io import current_file_io

        provider = current_file_io()
        if provider is not None:
            return provider.materialize(self, local_dir)

        return self.get_raw(context.config.resource, local_dir)

    def get_raw(self, local_resource: Resource, local_dir: Path | None = None) -> Path:
        """
        Copies the file stack to a local directory, assumes no context.

        :param local_resource: the local resource to copy the file stack to. Defaults to the current resource.
        :param local_dir: The local directory to copy the file stack to.
        :type local_dir: Path
        """

        if self.deleted_at is not None:
            raise ValueError("FileStack content has been deleted")

        from simstack.core.file_io import current_file_io
        from simstack.runner_protocol import RUNNER_CURRENT_RESOURCE_ALIAS

        provider = current_file_io()
        if provider is not None:
            requested_resource = getattr(local_resource, "__dict__", {}).get("value")
            provider_resource = getattr(provider.resource, "__dict__", {}).get("value")
            if requested_resource not in {provider_resource, RUNNER_CURRENT_RESOURCE_ALIAS}:
                raise ValueError("Artifact file I/O resource does not match the scoped provider")
            return provider.materialize(self, local_dir)

        # select the best instance
        # first search for an instance with "in_memory" set to True

        if local_dir is None:
            local_dir = Path.cwd()
        local_dir = Path(local_dir)

        from simstack.core.context import context

        if context.initialized and context.runner_mode:
            if not context.worker_mode:
                raise RuntimeError(
                    "FileStack materialization is forbidden in the supervisor process"
                )
            from simstack.core.runner_execution import current_runner_claim
            from simstack.runner_runtime.config import runner_task_workdir

            claim = current_runner_claim()
            allowed_task_dir = runner_task_workdir(
                Path(context.config.workdir), claim.task_id
            )
            resolved_local_dir = local_dir.resolve()
            try:
                resolved_local_dir.relative_to(allowed_task_dir)
            except ValueError as exc:
                raise ValueError(
                    "FileStack local_dir escapes the active runner task directory"
                ) from exc
            local_dir = resolved_local_dir

        if self.in_memory:
            return self.materialize_inline(local_dir)

        local_resource_name = transfer_resource_name(local_resource)
        local_transfer_resource = Resource(value=local_resource_name)

        # If in-memory instance not found or decompression failed, try finding instance on same resource
        for same_resource_instance in self.locations:
            if (
                transfer_resource_name(same_resource_instance.resource) != local_resource_name
                or same_resource_instance.status != "available"
            ):
                continue
            path = resolve_instance_path(
                same_resource_instance.path, Path(context.config.workdir)
            )
            if not path.is_file():
                continue
            if (
                same_resource_instance.size_bytes is not None
                and path.stat().st_size != same_resource_instance.size_bytes
            ) or (
                same_resource_instance.checksum_sha256 is not None
                and hash_file(path) != same_resource_instance.checksum_sha256.lower()
            ):
                raise ValueError("Local FileInstance no longer matches immutable content metadata")
            same_resource_instance.last_accessed_at = datetime.now()
            logger.info(f"Using existing instance {path} for {self.name}")
            return path

        remote_instance = first_available_remote_location(
            self.locations, local_transfer_resource
        )
        if remote_instance is None:
            logger.error("No suitable file instance found for copying.")
            raise ValueError(
                f"FileStack {self.id} {self.name} is unavailable: no accessible file instance for resource {local_transfer_resource}."
            )

        local_dir.mkdir(parents=True, exist_ok=True)
        return self._get_via_server_transfer(
            remote_instance, local_transfer_resource, local_dir
        )

    def materialize_inline(self, local_dir: Path) -> Path:
        """Publish compressed inline content atomically, without a runtime context."""
        if self.deleted_at is not None:
            raise ValueError("FileStack content has been deleted")
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            if not self.in_memory or self.content is None:
                raise ValueError("FileStack has no inline content")
            target_path = local_dir / _strict_file_name(self.name)
            _materialize_compressed_file(self.content, target_path)
            return target_path
        except Exception as exc:
            logger.error("Failed to materialize inline file %s: %s", self.name, exc)
            raise ValueError(f"Failed to decompress and write file {self.name}: {exc}") from exc

    def _get_via_server_transfer(
        self,
        remote_instance: FileInstance,
        local_resource: Resource,
        local_dir: Path,
    ) -> Path:
        from simstack.core.context import context
        from simstack.core.runner_execution import current_runner_claim

        client = ExecutionFileTransferClient.from_context()
        claim = current_runner_claim()
        source_file_instance_id = getattr(remote_instance, "id", None)
        if (
            not isinstance(source_file_instance_id, str)
            or not source_file_instance_id
            or source_file_instance_id != source_file_instance_id.strip()
        ):
            raise ValueError(
                "API runner FileStack transfer requires a persisted source "
                "FileInstance id"
            )

        transfer = client.create_transfer(
            task_id=claim.task_id,
            claim_id=claim.claim_id,
            attempt=claim.attempt,
            file_stack_id=str(self.id),
            source_file_instance_id=str(source_file_instance_id),
            source_resource_name=transfer_resource_name(remote_instance.resource),
            target_resource_name=transfer_resource_name(local_resource),
        )
        restarted = set()
        while True:
            transfer_id = str(transfer["transfer_id"])
            if transfer.get("status") == FileTransferStatus.COMPLETED:
                completed = client.get_transfer(transfer_id)
                cached_path = self._completed_transfer_path(
                    completed, local_resource, claim.task_id
                )
                if cached_path is not None:
                    return cached_path
            elif transfer.get("status") not in {
                FileTransferStatus.FAILED, FileTransferStatus.EXPIRED
            }:
                try:
                    ready = client.wait_until_uploaded(transfer_id)
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
                if ready.get("status") != FileTransferStatus.COMPLETED:
                    break
                # Another materialization can finish while this caller waits.
                transfer = ready
                continue

            if transfer_id in restarted:
                raise FileTransferError("File transfer restart returned a cyclic successor")
            restarted.add(transfer_id)
            transfer = client.restart_transfer(transfer_id)

        file_name = _strict_file_name(self.name)
        target_path = local_dir / file_name
        if target_path.exists():
            raise FileExistsError(f"File {target_path} already exists.")
        downloaded = client.download_file(transfer_id, target_path)
        instance_path = path_for_file_instance(downloaded.path, context.config.workdir)
        completed = client.complete_transfer(
            transfer_id=transfer_id,
            target_resource_name=transfer_resource_name(local_resource),
            target_path=instance_path,
            size_bytes=downloaded.size_bytes,
            checksum_sha256=downloaded.checksum_sha256,
        )

        self._remember_transfer_location(
            file_instance_id=completed.get("file_instance_id"),
            instance_path=instance_path,
            local_resource=local_resource,
            size_bytes=downloaded.size_bytes,
            checksum_sha256=downloaded.checksum_sha256,
        )
        return downloaded.path

    def _completed_transfer_path(
        self, completed: dict, local_resource: Resource, task_id: str
    ) -> Path | None:
        """Reuse an API-owned cache only after task/path/content verification."""
        from simstack.core.context import context
        from simstack.runner_runtime.config import runner_task_workdir

        instance_path = completed.get("target_path")
        size_bytes = completed.get("size_bytes")
        checksum = completed.get("checksum_sha256")
        if (
            not isinstance(instance_path, str)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum)
        ):
            raise FileTransferError("Completed file transfer has incomplete immutable metadata")
        path = resolve_instance_path(instance_path, context.config.workdir)
        try:
            path.relative_to(runner_task_workdir(context.config.workdir, task_id))
        except ValueError as exc:
            raise FileTransferError("Completed transfer path escapes the active task directory") from exc
        if not path.exists():
            return None
        if (
            not path.is_file()
            or path.stat().st_size != size_bytes
            or hash_file(path) != checksum.lower()
        ):
            raise FileTransferError("Completed transfer cache no longer matches immutable metadata")
        self._remember_transfer_location(
            file_instance_id=completed.get("file_instance_id"),
            instance_path=instance_path,
            local_resource=local_resource,
            size_bytes=size_bytes,
            checksum_sha256=checksum.lower(),
        )
        return path

    def _remember_transfer_location(
        self, *, file_instance_id: str | None, instance_path: str,
        local_resource: Resource, size_bytes: int, checksum_sha256: str,
    ) -> None:
        if (
            not isinstance(file_instance_id, str)
            or not file_instance_id
            or file_instance_id != file_instance_id.strip()
        ):
            raise ValueError(
                "File transfer completion requires a persisted target FileInstance id"
            )
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
                    id=file_instance_id,
                    path=instance_path,
                    resource=local_resource,
                    created_at=datetime.now(),
                    size_bytes=size_bytes,
                    checksum_sha256=checksum_sha256,
                    location_type="local_path",
                    is_cached=True,
                    status="available",
                )
            )

    def str(self) -> str:
        return f"FileStack(name={self.name}, size={self.size}, is_hashable={self.is_hashable}, in_memory={self.in_memory}, locations={self.locations})"
