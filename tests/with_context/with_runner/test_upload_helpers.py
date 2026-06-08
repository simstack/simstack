import pytest
import zipfile
import tarfile
from pathlib import Path
from simstack.methods.upload_helpers import (
    archive_upload,
    file_list_upload_test,
    file_list_upload_all,
)
from simstack.models import Parameters, FileListModel
from simstack.models.files import FileStack


@pytest.fixture
def temp_dir(tmp_path):
    return tmp_path


@pytest.mark.asyncio
async def test_archive_upload_zip(temp_dir):
    # Create a dummy zip file
    file1 = temp_dir / "test1.txt"
    file1.write_text("content1")
    zip_path = temp_dir / "test.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.write(file1, arcname="test1.txt")

    file_stack = FileStack.from_local_file(zip_path)
    parameters = Parameters(resource="test", force_rerun=True)

    result = await archive_upload(file_stack, parameters=parameters)

    assert isinstance(result, FileListModel)
    assert len(result) == 1
    extracted_file_stack = await result.get(0)
    extracted_file = extracted_file_stack.get(local_dir=temp_dir)
    assert Path(extracted_file).name == "test1.txt"
    with open(extracted_file, "r") as f:
        assert f.read() == "content1"


@pytest.mark.asyncio
async def test_archive_upload_tar_gz(temp_dir):
    # Create a dummy tar.gz file
    file2 = temp_dir / "test2.txt"
    file2.write_text("content2")
    tar_path = temp_dir / "test.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(file2, arcname="test2.txt")

    file_stack = FileStack.from_local_file(tar_path)
    parameters = Parameters(resource="test", force_rerun=True)

    result = await archive_upload(file_stack, parameters=parameters)

    assert isinstance(result, FileListModel)
    assert len(result) == 1
    extracted_file_stack = await result.get(0)
    extracted_file = extracted_file_stack.get(local_dir=temp_dir)
    assert Path(extracted_file).name == "test2.txt"
    with open(extracted_file, "r") as f:
        assert f.read() == "content2"


@pytest.mark.asyncio
async def test_archive_upload_regular_file(temp_dir):
    # Create a regular file
    file3 = temp_dir / "test3.txt"
    file3.write_text("content3")

    file_stack = FileStack.from_local_file(file3)
    parameters = Parameters(resource="test", force_rerun=True)

    result = await archive_upload(file_stack, parameters=parameters)

    assert isinstance(result, FileListModel)
    assert len(result) == 1
    file_stack_result = await result.get(0)
    assert file_stack_result.id == file_stack.id


@pytest.mark.asyncio
async def test_file_list_upload_test(temp_dir):
    file4 = temp_dir / "test4.txt"
    file4.write_text("content4")
    file_stack = FileStack.from_local_file(file4)
    file_list = FileListModel(field_name="test_files")
    await file_list.append(file_stack)

    parameters = Parameters(resource="test", force_rerun=True)
    result = await file_list_upload_test(file_list, parameters=parameters)

    assert result is True


@pytest.mark.asyncio
async def test_file_list_upload_all(temp_dir):
    # Create a zip file for upload_all
    file5 = temp_dir / "test5.txt"
    file5.write_text("content5")
    zip_path = temp_dir / "all.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.write(file5, arcname="test5.txt")

    file_stack = FileStack.from_local_file(zip_path)
    parameters = Parameters(resource="test", force_rerun=True)

    result = await file_list_upload_all(file_stack, parameters=parameters)

    assert result is True
