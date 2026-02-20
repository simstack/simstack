import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Union

from odmantic import EmbeddedModel, Field, ObjectId
from pydantic import model_validator

from simstack.models import simstack_model
from simstack.models.parameters import Resource

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

    path: str = Field(description="Path to the file relative to the host work directory")
    resource: Resource = Field(description="Resource name")
    created_at: datetime = Field(description="Creation timestamp")

    @model_validator(mode='before')
    def migration(cls, values):
        if isinstance(values.get('resource'), str):
            values['resource'] = Resource(value=values['resource'])
        if "path" in values and isinstance(values['path'], Path):
            values['path'] = str(values['path'])
        return values

    @classmethod
    def from_local_file(
        cls, path: Union[Path, str], file_stack_id: ObjectId, make_copy: bool = True
    ):
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
        source_path = path if isinstance(path, Path) else Path(path)

        # Prepare the content field if in_memory is True
        resolved_path = Path(path).resolve()
        # Find 'simstack' in the path and compute the relative path from its parent
        from simstack.core.context import context
        workdir = context.config.workdir
        resolved_workdir = Path(workdir).resolve()
        logger.debug(f"workdir is {resolved_workdir} path is {resolved_path}")

        try:
            relative_path = resolved_path.relative_to(resolved_workdir)
        except ValueError:
            logger.debug(f"Path {resolved_path} is not under workdir {resolved_workdir}")
            import getpass
            username = getpass.getuser()
            relative_path = Path(username) / str(file_stack_id)
            absolute_dir = Path(context.config.workdir) / relative_path
            absolute_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(source_path, absolute_dir)

        file_instance = FileInstance(
            path=str(relative_path),
            resource=context.config.resource,
            created_at=datetime.now(),
        )
        return file_instance
