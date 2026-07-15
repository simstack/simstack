import pytest
import os
import shutil
from pathlib import Path
from simstack.core.context import context
from simstack.models import FileStack, BooleanData
from simstack.methods.archive_files import archive_node, ArchiveConfig, archive_one_file
from simstack.models.base_lists import BooleanDataList
from simstack.core.definitions import TaskStatus

@pytest.mark.asyncio
async def test_archive_node_e2e(tmp_path, mocker):
    db = context.db
    # Reset DB for e2e
    await db.delete_all(FileStack) if hasattr(db, "delete_all") else None
    for fs_inst in await db.find(FileStack): await db.delete(fs_inst)
    
    workdir = Path(context.config.workdir)
    
    # 1. Create a file and a FileStack
    test_file = workdir / "e2e_test.txt"
    test_file.write_text("e2e content")
    fs = FileStack.from_local_file(test_file)
    fs.name = "e2e_test.txt"
    await db.save(fs)
    
    # 2. Setup archive path in config
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    
    # Use real BooleanData for results to avoid list issues
    from simstack.models import BooleanData
    async def mock_archive_files_real(file_list, **kwargs):
        res = BooleanDataList(field_name="archive_results")
        for fs in file_list:
            res.append(BooleanData(value=archive_one_file(fs, **kwargs)))
        return res
    
    import simstack.methods.archive_files
    mocker.patch("simstack.methods.archive_files.archive_files", side_effect=mock_archive_files_real)

    # We need to inject this into context.resource_config
    # Usually it's read from config.toml
    # For e2e test, we mock the config getter
    from simstack.util.resource_config import ResourceConfig
    orig_get_program = context.resource_config.get_program
    
    def mock_get_program(name):
        if name == "archive_one_file":
            return {"archive_path": str(archive_dir)}
        return orig_get_program(name)
    
    context.resource_config.get_program = mock_get_program
    
    try:
        # 3. Run archive_node
        config = ArchiveConfig(include_patterns=["e2e_test.txt"])
    
        # Reset ArchiveConfig defaults for test
        config.start_date = None
        config.end_date = None
    
        # We need a real NodeRunner mock that supports logging and task_id
        from simstack.core.node_runner import NodeRunner
        import logging
        mock_runner = NodeRunner(name="e2e_runner", task_id="e2e_task", logger=logging.getLogger("test"))
        
        results = await archive_node._inner(config, node_runner=mock_runner)
        
        # 4. Verify results
        assert len(results) == 1
        assert results[0].value is True
        
        # 5. Verify file was archived
        archived_file = archive_dir / str(fs.id) / "e2e_test.txt"
        assert archived_file.exists()
        assert archived_file.read_text() == "e2e content"
        
        # 6. Verify local file was deleted
        assert not test_file.exists()
        
        # 7. Verify FileStack locations were updated in DB
        updated_fs = await db.find_one(FileStack, FileStack.id == fs.id)
        assert len(updated_fs.locations) == 1
        assert updated_fs.locations[0].location_type == "local_path"
        # The new location should be the one in the archive_dir (relative to workdir or absolute?)
        # archive_one_file uses FileInstance.from_local_file(archive_location)
        
    finally:
        context.resource_config.get_program = orig_get_program
        if test_file.exists(): test_file.unlink()
