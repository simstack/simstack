import zipfile
import tarfile
import os
from pathlib import Path

from simstack.core.node import node
from simstack.models import FileListModel
from simstack.models.files import FileStack


@node
async def archive_upload(file_upload: FileStack, **kwargs):
    """
    Asynchronously processes a file upload, extracts files from supported archive formats
    (.zip, .tar, .tar.gz), and returns a list of extracted files wrapped in FileStack
    instances. If the file is not an archive, it is added as-is to the output.

    Args:
        file_upload (FileStack): The uploaded file to be processed.
        **kwargs: Additional keyword arguments.
            node_runner: An optional object for logging or processing context. If supplied,
            it will log file paths and extraction details using its `info` method.

    Returns:
        FileListModel: A model containing a list of FileStack instances representing
        the extracted files or the original file if it is not an archive.
    """
    node_runner = kwargs.get("node_runner", None)
    local_file = file_upload.get()

    local_file_path = Path(local_file)
    node_runner.info(f"Local file path: {local_file_path}")
    extract_dir = local_file_path.parent

    output_file_list = FileListModel(field_name="extracted_files")
    if local_file_path.suffix == ".zip":
        with zipfile.ZipFile(local_file_path, "r") as zip_ref:
            node_runner.info(
                f"Extracting files from {local_file_path} to {extract_dir}"
            )
            zip_ref.extractall(extract_dir)

        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                file_path = Path(root) / file
                if file_path != local_file_path:
                    node_runner.info(f"Storing extracted file: {file_path}")
                    file_stack = FileStack.from_local_file(
                        file_path, in_memory=True, is_hashable=True, secure_source=True
                    )
                    await output_file_list.append(file_stack)

    elif local_file_path.suffix == ".tar" or local_file_path.name.endswith(".tar.gz"):
        with tarfile.open(local_file_path, "r:*") as tar_ref:
            node_runner.info(
                f"Extracting files from {local_file_path} to {extract_dir}"
            )
            tar_ref.extractall(extract_dir)

        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                file_path = Path(root) / file
                if file_path != local_file_path:
                    node_runner.info(f"Storing extracted file: {file_path}")
                    file_stack = FileStack.from_local_file(
                        file_path, in_memory=True, is_hashable=True, secure_source=True
                    )
                    await output_file_list.append(file_stack)
    else:
        await output_file_list.append(file_upload)

    return output_file_list


@node
async def file_list_upload_test(file_list: FileListModel, **kwargs):
    node_runner = kwargs.get("node_runner", None)
    async for file_stack in file_list:
        local_file = file_stack.get()
        node_runner.info(f"Local file path: {local_file}")
    return True


@node
async def file_list_upload_all(file_stack: FileStack, **kwargs):
    file_list = await archive_upload(file_stack, **kwargs)
    unpacked = await file_list_upload_test(file_list, **kwargs)
    return unpacked
