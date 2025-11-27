import logging
import os
import tempfile
import zlib
from pathlib import Path
from typing import List, Optional, Union, Dict, Any

from odmantic import Model, Field, ObjectId


from simstack.models import simstack_model
from simstack.models.file_instance import FileInstance
from simstack.models.parameters import Resource
from simstack.util.file_hashing import hash_file

logger = logging.getLogger(__name__)


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

    def __str__(self):
        return f"FileStack(name={self.name}, size={self.size}, is_hashable={self.is_hashable}, in_memory={self.in_memory}, locations={self.locations})"

    def __repr__(self):
        return f"FileStack(name={self.name}, size={self.size}, is_hashable={self.is_hashable}, in_memory={self.in_memory}, locations={self.locations})"

    async def custom_model_dump(self, **kwargs) -> Dict[str, Any]:
        dumped_data = self.model_dump()
        del dumped_data["content"]  # Exclude content from the dumped data
        return dumped_data

    @classmethod
    def ui_base_schema(cls, **kwargs) -> Dict[str, Any]:
        # TODO get the model programatically
        return {
            "ui:field": "FileField",
            "ui:options": {
                "model": "simstack.models.files.FileStack",
            },
        }

    @classmethod
    def from_local_file(
        cls,
        path: Union[Path, str],
        is_hashable: bool = True,
        in_memory: bool = True,
        secure_source: bool = False,
        task_id: str = "",
    ):
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

        content = None
        file_hash = hash_file(source_path) if is_hashable else None
        name = source_path.name
        size = source_path.stat().st_size if source_path.exists() else None

        if in_memory:
            try:
                # Read the file content
                with open(source_path, "rb") as f:
                    file_content = f.read()
                # Compress the content using zlib
                content = zlib.compress(file_content)
                logger.debug(
                    f"Compressed file {source_path} from {len(file_content)} bytes to {len(content)} bytes"
                )
            except Exception as e:
                logger.warning(f"Failed to compress file {source_path}: {e}")

        file_stack = cls(
            name=name,
            in_memory=in_memory,
            content=content,
            is_hashable=is_hashable,
            size=size,
            hash=file_hash,
        )

        if not in_memory:
            location = FileInstance.from_local_file(
                path=path, file_stack_id=file_stack.id, make_copy=not secure_source
            )
            file_stack.locations.append(location)
        return file_stack

    def complex_hash(self) -> str:
        if self.is_hashable:
            if self.hash:
                return self.hash
            else:
                raise ValueError("FileStack is hashable but hash is not set.")
            #elif self.in_memory and self.content:
            #    # If the content is in memory, hash the compressed content
            #    return complex_hash_function(zlib.decompress(self.content))
            #else:
            #    temp_dir = Path(tempfile.mkdtemp())
            #    local_file = self.get(None, local_dir=temp_dir)
            #    return complex_hash_function(local_file.read_bytes())
        else:
            logger.warning(
                f"FileStack {self.id} is not hashable, returning unique hash."
            )
            return str(ObjectId())

    def append(self, file_instance: FileInstance):
        """
        Appends a FileInstance to the file stack.

        :param file_instance: The FileInstance to append.
        :type file_instance: FileInstance
        """
        self.locations.append(file_instance)

    def get(self, local_resource: Resource, local_dir: Path) -> Path:
        """
        Copies the file stack to a local directory.

        :param local_resource: the local resource to copy the file stack to. Defaults to the current resource.
        :param local_dir: The local directory to copy the file stack to.
        :type local_dir: Path
        """

        # select the best instance
        # first search for an instance with "in_memory" set to True

        if self.in_memory:
            local_dir.mkdir(parents=True, exist_ok=True)
            try:
                # Decompress the content
                decompressed_content = zlib.decompress(self.content)
                # Write the decompressed content to the local directory
                with open(local_dir / self.name, "wb") as f:
                    f.write(decompressed_content)
                return local_dir / self.name
            except Exception as e:
                logger.error(f"Failed to decompress and write file {self.name}: {e}")
                raise ValueError(
                    f"Failed to decompress and write file {self.name}: {e}"
                )
        # If in-memory instance not found or decompression failed, try finding instance on same resource
        if same_resource_instance := next(
            (f for f in self.locations if f.resource == local_resource), None
        ):
            # Copy the file from the source path to the destination path
            return Path(same_resource_instance.path)
        else:
            local_dir.mkdir(parents=True, exist_ok=True)
            logger.error("No suitable file instance found for copying.")
            from simstack.methods.get_file import get_file
            return get_file(self,local_resource, local_dir / self.name)


    def str(self):
        return f"FileStack(name={self.name}, size={self.size}, is_hashable={self.is_hashable}, in_memory={self.in_memory}, locations={self.locations})"


async def main():
    from simstack.core.context import context

    context.initialize()
    # write a file test.txt
    with open("test.txt", "w") as f:
        f.write("Hello World")
    file_stack = FileStack.from_local_file("test.txt", is_hashable=True, in_memory=True)
    print(file_stack)
    await context.db.save(file_stack)
    local_dir = Path(context.config.workdir) / "samira" / str(file_stack.id)
    retrieved = file_stack.get(context.config.resource, local_dir=local_dir)
    print("Retrieved file path:", retrieved)


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())
