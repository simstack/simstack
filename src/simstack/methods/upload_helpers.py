import zipfile
import tarfile
from pathlib import Path

from simstack.core.context import context
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
    node_runner = kwargs.get('node_runner', None)
    local_file = file_upload.get()

    local_file_path = Path(local_file)
    node_runner.info(f"Local file path: {local_file_path}")
    extract_dir = local_file_path.parent

    output_file_list = FileListModel(field_name="extracted_files")
    if local_file_path.suffix == '.zip':
        with zipfile.ZipFile(local_file_path, 'r') as zip_ref:
            node_runner.info(f"Extracting files from {local_file_path} to {extract_dir}")
            extracted_files = [extract_dir / member.filename for member in zip_ref.infolist() if not member.is_dir()]
            zip_ref.extractall(extract_dir)
    elif local_file_path.suffix == '.tar' or local_file_path.name.endswith('.tar.gz'):
        with tarfile.open(local_file_path, 'r:*') as tar_ref:
            node_runner.info(f"Extracting files from {local_file_path} to {extract_dir}")
            extracted_files = [extract_dir / member.name for member in tar_ref.getmembers() if not member.isdir()]
            tar_ref.extractall(extract_dir)
    else:
        output_file_list.append(file_upload)
        return output_file_list

    for file_path in dict.fromkeys(extracted_files):
        node_runner.info(f"Storing extracted file: {file_path}")
        output_file_list.append(FileStack.from_local_file(
            file_path, is_hashable=True, secure_source=True))

    return output_file_list

@node
async def file_list_upload_test(file_list: FileListModel,**kwargs):
    node_runner = kwargs.get('node_runner', None)
    # Ensure cache is loaded before iterating synchronously
    for file_stack in file_list:
        local_file = file_stack.get()
        node_runner.info(f"Local file path: {local_file}")
    return True

@node
async def file_list_upload_all(file_stack: FileStack,**kwargs):
    file_list = await archive_upload(file_stack, **kwargs)
    unpacked = await file_list_upload_test(file_list, **kwargs)
    return unpacked

