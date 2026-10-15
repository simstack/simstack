from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from simstack.models.document import EmbeddedModel, Field, ObjectId
from pydantic import field_validator, model_validator

from simstack.models import simstack_model
from simstack.models.parameters import Resource
from simstack.runner_api.file_transfer import transfer_resource_name
from simstack.util.file_hashing import hash_file

logger = logging.getLogger("file_instance")


@simstack_model
class FileInstance(EmbeddedModel):
    """
    Represents an embedded model for a file instance.

    The `FileInstance` class is used to encapsulate details about a file,
    including its path, associated resource, and creation timestamp.
    It provides a class method for initializing a `FileInstance` object
    from a local file path, ensuring proper handling of file-related operations.

    Attributes:
        path (str): Path to the file relative to the host work directory.
        resource (Resource): Name of the resource associated with the file.
        created_at (datetime): Timestamp indicating when the file instance was created.
    """

    id: Optional[str] = Field(default=None, description="File instance id")
    path: str = Field(
        description="Path to the file relative to the host work directory"
    )
    resource: Resource = Field(description="Resource name")
    created_at: datetime = Field(description="Creation timestamp")
    runner_id: Optional[str] = Field(
        default=None, description="Runner identity that created the instance"
    )
    location_type: str = Field(default="local_path", description="File location type")
    size_bytes: Optional[int] = Field(default=None, description="File size in bytes")
    checksum_sha256: Optional[str] = Field(default=None, description="SHA256 checksum")
    last_accessed_at: Optional[datetime] = Field(
        default=None, description="Last access timestamp"
    )
    expires_at: Optional[datetime] = Field(
        default=None, description="Expiration timestamp"
    )
    is_authoritative: bool = Field(
        default=True, description="Whether this instance is authoritative"
    )
    is_cached: bool = Field(
        default=False,
        description="Whether this instance is a reusable local cache copy",
    )
    status: str = Field(default="available", description="Instance lifecycle status")

    @field_validator("id")
    @classmethod
    def validate_persisted_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("File instance id must be an opaque non-empty string")
        return value

    @model_validator(mode="before")
    def migration(cls, values: Any) -> Any:
        if isinstance(values.get("resource"), str):
            values["resource"] = Resource(value=values["resource"])
        if "path" in values and isinstance(values["path"], Path):
            values["path"] = str(values["path"])
        if not values.get("location_type"):
            values["location_type"] = "local_path"
        if not values.get("status"):
            values["status"] = "available"
        if values.get("is_authoritative") is None:
            values["is_authoritative"] = True
        if values.get("is_cached") is None:
            values["is_cached"] = False
        return values

    @classmethod
    def from_local_file(
        cls,
        path: Union[Path, str],
        file_stack_id: ObjectId,
        make_copy: bool = True,
        tasks_id: str = "",
        *,
        workdir: Path | str | None = None,
        resource: Resource | None = None,
    ) -> FileInstance:
        """
        Creates a FileInstance object from a local file path.

        This class method is responsible for initializing a `FileInstance` based
        on a local file's path. It supports options to hash the file, make a
        user-specific copy, and tracks additional metadata. The method handles
        local file operations such as copying files to a secure directory when
        necessary and organizes resources under a configurable working directory.

        :param file_stack_id: the id of the filestack where the file is in
        :param path: The file path to the local file. Can be either a string
            or `Path`.
        :param make_copy: Indicates whether a secure local copy of the file should
            be made within the application's working directory. Defaults to True.
        :return: A `FileInstance` object initialized with file details.
        :rtype: FileInstance
        :raises ValueError: If there are issues during the creation of the
            `FileInstance` from the specified local file.
        """
        source_path = Path(path).resolve(strict=True)
        if not source_path.is_file():
            raise ValueError(f"FileInstance source is not a file: {source_path}")
        if (workdir is None) != (resource is None):
            raise ValueError(
                "FileInstance requires both workdir and resource, or runner context"
            )
        from simstack.core.file_io import current_file_io

        provider = current_file_io()
        if workdir is None:
            from simstack.core.context import context

            if provider is not None:
                workdir, resource = provider.workdir, provider.resource
            else:
                workdir = context.config.workdir
                resource = context.config.resource
        resolved_workdir = Path(workdir).resolve()
        try:
            relative_path = source_path.relative_to(resolved_workdir)
        except ValueError:
            # A location must remain usable by the resource's upload service;
            # an external absolute path cannot be persisted as a location.
            make_copy = True
        if make_copy:
            # A scoped artifact port exposes only its request-local output
            # directory. Explicit context passed by FileStack must not bypass it.
            file_root = (
                provider.staging_dir
                if provider is not None
                and resolved_workdir == Path(provider.workdir).resolve()
                and transfer_resource_name(resource)
                == transfer_resource_name(provider.resource)
                else resolved_workdir / "files"
            )
            file_root.mkdir(parents=True, exist_ok=True)
            absolute_dir = Path(tempfile.mkdtemp(prefix="instance-", dir=file_root))
            stored_path = absolute_dir / source_path.name
            try:
                shutil.copyfile(source_path, stored_path)
            except Exception:
                stored_path.unlink(missing_ok=True)
                absolute_dir.rmdir()
                raise
            relative_path = stored_path.relative_to(resolved_workdir)
        else:
            stored_path = source_path

        file_instance = FileInstance(
            id=str(uuid.uuid4()),
            path=str(relative_path),
            resource=Resource(value=transfer_resource_name(resource)),
            created_at=datetime.now(),
            size_bytes=stored_path.stat().st_size,
            checksum_sha256=hash_file(stored_path),
        )
        return file_instance
