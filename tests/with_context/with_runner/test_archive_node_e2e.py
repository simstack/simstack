from pathlib import Path

import pytest

from simstack.methods.archive_files import (
    ArchiveConfig,
    ArchiveFeatureDisabledError,
    archive_file,
    archive_files,
    archive_node,
    archive_one_file,
)
from simstack.models import FileList, FileStack


@pytest.mark.asyncio
async def test_archive_entrypoints_fail_closed_without_touching_files(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("authoritative content")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    file_stack = FileStack.from_local_file(source, in_memory=False)
    file_list = FileList(field_name="archive_files")

    with pytest.raises(ArchiveFeatureDisabledError, match="disabled"):
        archive_one_file(file_stack)
    with pytest.raises(ArchiveFeatureDisabledError, match="disabled"):
        await archive_file._inner(file_stack)
    with pytest.raises(ArchiveFeatureDisabledError, match="disabled"):
        archive_files._inner(file_list)
    with pytest.raises(ArchiveFeatureDisabledError, match="disabled"):
        await archive_node._inner(ArchiveConfig())

    assert source.read_text() == "authoritative content"
    assert list(archive_dir.iterdir()) == []
