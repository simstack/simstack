import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Union

from odmantic import EmbeddedModel, Field, ObjectId

from simstack.models import simstack_model

logger = logging.getLogger("file_instance")


@simstack_model
class FileInstance(EmbeddedModel):
    """ """

    path: str = Field(
        description="Path to the file relative to the host work directory"
    )
    resource: str = Field(description="Resource name")

    created_at: datetime = Field(description="Creation timestamp")

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

        try:
            from simstack.core.context import context

            if make_copy:
                # make a local copy
                import getpass

                username = getpass.getuser()
                relative_path = Path(username) / str(file_stack_id)
                absolute_dir = Path(context.config.workdir) / relative_path
                absolute_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, absolute_dir)
            else:
                relative_path = Path(path).relative_to(context.config.workdir)

            file_instance = FileInstance(
                path=str(relative_path / source_path.name),
                resource=context.config.resource,
                created_at=datetime.now(),
            )
            return file_instance
        except Exception as e:
            logger.error(f"Error creating FileInstance from local file {path}: {e}")
            raise ValueError(
                f"Could not create FileInstance from local file {path}: {e}"
            )
