import asyncio
import pytest
import pytest_asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from simstack.core.definitions import TaskStatus
from simstack.core.context import context
from simstack.models import FileStack, FileList, NodeRegistry, FileListModel, BooleanData
from simstack.models.file_instance import FileInstance
from simstack.models.parameters import Resource
from simstack.methods.archive_files import archive_node, ArchiveConfig
from simstack.models.base_lists import BooleanDataList

pytestmark = pytest.mark.skip(
    reason="archive prototype is explicitly excluded from this migration"
)

@pytest_asyncio.fixture
async def sample_files():
    db = context.db
    # Clear DB to avoid side effects from other tests
    from simstack.models import FileStack, NodeRegistry, Parameters
    # db.delete_all is not available, use find and delete
    for model in [FileStack, NodeRegistry, Parameters]:
        instances = await db.find(model)
        for inst in instances:
            await db.delete(inst)
    
    # Check if empty
    assert len(await db.find(FileStack)) == 0

    workdir = Path(context.config.workdir)
    my_resource = context.config.resource

    # Create some local files
    f1_path = workdir / "file1.txt"
    f1_path.write_text("content1")
    fs1 = FileStack.from_local_file(f1_path)
    fs1.name = "file1.txt"
    fs1.size = 10
    from simstack.models.file_instance import FileInstance
    from simstack.models.parameters import Resource
    # Ensure it has a location
    if not fs1.locations:
        fs1.locations.append(FileInstance(
            path=str(f1_path.relative_to(workdir)),
            resource=Resource(value=str(context.config.resource)),
            created_at=datetime.now()
        ))
    await db.save(fs1)

    f2_path = workdir / "file2.log"
    f2_path.write_text("content2 has more data")
    fs2 = FileStack.from_local_file(f2_path)
    fs2.name = "file2.log"
    fs2.size = 100
    if not fs2.locations:
        fs2.locations.append(FileInstance(
            path=str(f2_path.relative_to(workdir)),
            resource=Resource(value=str(context.config.resource)),
            created_at=datetime.now()
        ))
    await db.save(fs2)

    # File on another resource
    fs3 = FileStack(name="other_resource.txt", size=50)
    from simstack.models.file_instance import FileInstance
    from simstack.models.parameters import Resource
    fs3.locations.append(FileInstance(
        path="other_resource.txt",
        resource=Resource(value="other"),
        created_at=datetime.now()
    ))
    await db.save(fs3)
    
    stacks = await db.find(FileStack)
    print(f"DEBUG: sample_files created {len(stacks)} stacks")
    for s in stacks:
        print(f"DEBUG: stack {s.name} size={s.size} locations={len(s.locations)}")

    yield [fs1, fs2, fs3]

    # Cleanup
    if f1_path.exists(): f1_path.unlink()
    if f2_path.exists(): f2_path.unlink()

@pytest.mark.asyncio
async def test_archive_node_filter_by_size(sample_files, mocker):
    async def mock_archive_files(*a, **k):
        return []
    
    import simstack.methods.archive_files
    print(f"DEBUG: archive_node identity: {id(simstack.methods.archive_files.archive_node)}")
    
    mock_archive1 = mocker.patch("simstack.methods.archive_files.archive_files", side_effect=mock_archive_files)
    
    config = ArchiveConfig(min_size=50)
    mock_node_runner = mocker.MagicMock()
    
    # Call undecorated function
    print("DEBUG: Pre-call")
    await simstack.methods.archive_files.archive_node._inner(config, node_runner=mock_node_runner)
    print("DEBUG: Post-call")
    
    assert mock_archive1.called
    args, kwargs = mock_archive1.call_args
    file_list = args[0]
    assert len(file_list) == 2
    names = [fs.name for fs in file_list]
    assert "file2.log" in names
    assert "other_resource.txt" in names
    assert "file1.txt" not in names

@pytest.mark.asyncio
async def test_archive_node_filter_by_resource(sample_files, mocker):
    async def mock_archive_files(*a, **k): return []
    import simstack.methods.archive_files
    mock_archive = mocker.patch("simstack.methods.archive_files.archive_files", side_effect=mock_archive_files)
    
    config = ArchiveConfig(filter_by_resource=True)
    mock_node_runner = mocker.MagicMock()
    
    await archive_node._inner(config, node_runner=mock_node_runner)
    
    assert mock_archive.called
    args, kwargs = mock_archive.call_args
    file_list = args[0]
    # Only fs1 and fs2 are on the current resource (test)
    assert len(file_list) == 2
    names = [fs.name for fs in file_list]
    assert "file1.txt" in names
    assert "file2.log" in names
    assert "other_resource.txt" not in names

@pytest.mark.asyncio
async def test_archive_node_filter_by_patterns(sample_files, mocker):
    async def mock_archive_files(*a, **k): return []
    import simstack.methods.archive_files
    mock_archive = mocker.patch.object(simstack.methods.archive_files, "archive_files", side_effect=mock_archive_files)
    
    # Include only .txt files
    config = ArchiveConfig(include_patterns=["*.txt"])
    mock_node_runner = mocker.MagicMock()
    
    await archive_node._inner(config, node_runner=mock_node_runner)
    
    assert mock_archive.called
    args, kwargs = mock_archive.call_args
    file_list = args[0]
    assert len(file_list) == 2
    names = [fs.name for fs in file_list]
    assert "file1.txt" in names
    assert "other_resource.txt" in names
    assert "file2.log" not in names

    # Exclude other_resource.txt
    mock_archive.reset_mock()
    config = ArchiveConfig(include_patterns=["*.txt"], exclude_patterns=["other*"])
    await archive_node._inner(config, node_runner=mock_node_runner)
    assert mock_archive.called
    args, kwargs = mock_archive.call_args
    file_list = args[0]
    assert len(file_list) == 1
    assert file_list[0].name == "file1.txt"

@pytest.mark.asyncio
async def test_archive_node_filter_by_date(sample_files, mocker):
    async def mock_archive_files(*a, **k): return []
    import simstack.methods.archive_files
    mock_archive = mocker.patch("simstack.methods.archive_files.archive_files", side_effect=mock_archive_files)
    
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    tomorrow = now + timedelta(days=1)
    
    config = ArchiveConfig(start_date=tomorrow)
    mock_node_runner = mocker.MagicMock()
    
    await archive_node._inner(config, node_runner=mock_node_runner)
    # No files should match
    assert not mock_archive.called

    config = ArchiveConfig(start_date=yesterday, end_date=tomorrow)
    await archive_node._inner(config, node_runner=mock_node_runner)
    assert mock_archive.called
    assert len(mock_archive.call_args[0][0]) == 3

@pytest.mark.asyncio
async def test_archive_node_filter_by_call_paths(sample_files, mocker):
    async def mock_archive_files(*a, **k): return []
    import simstack.methods.archive_files
    mock_archive = mocker.patch("simstack.methods.archive_files.archive_files", side_effect=mock_archive_files)
    
    db = context.db
    mock_node_runner = mocker.MagicMock()
    
    fs1, fs2, fs3 = sample_files
    
    # Create a NodeRegistry entry
    from simstack.models.named_data_reference import NamedDataReference
    from simstack.models.parameters import Parameters, Resource
    
    params = Parameters(resource=Resource(value=str(context.config.resource)))

    node = NodeRegistry(
        name="test_node",
        call_path="path/to/node",
        status=TaskStatus.COMPLETED,
        parameters=params,
        function_hash="abc",
        arg_hash="def",
        func_mapping="mapping"
    )
    # Add fs1 to info_files
    node.info_files.append(fs1)
    
    # Add fs2 to results_references
    filestack_mapping = context.model_mappings.get_by_name("FileStack")
    node.results_references.append(NamedDataReference(
        variable_name="result_file",
        variable_mapping=filestack_mapping.mapping,
        reference=fs2.id
    ))
    
    await db.save(node)
    
    config = ArchiveConfig(call_paths=["path/to/node"])
    await archive_node._inner(config, node_runner=mock_node_runner)
    
    assert mock_archive.called
    args, kwargs = mock_archive.call_args
    file_list = args[0]
    assert len(file_list) == 2
    names = [fs.name for fs in file_list]
    assert "file1.txt" in names
    assert "file2.log" in names
    assert "other_resource.txt" not in names
