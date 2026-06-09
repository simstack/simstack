import pytest
from typing import Optional
from odmantic import Model, Field
from simstack.models import simstack_model
from simstack.models.files import FileStack
from simstack.models import FileList
from simstack.core.context import context

@simstack_model
class SampleFileModel(Model):
    file_list: FileList = Field(default_factory=FileList)
    name: Optional[str] = None

    def __init__(self, **data):
        Model.__init__(self, **data)

@pytest.fixture
def sample_file_stacks():
    """Create multiple sample FileStacks for testing"""
    return [
        FileStack(
            name="file1.txt",
            size=50,
            is_hashable=True,
            in_memory=True,
            content=b"content1",
        ),
        FileStack(
            name="file2.txt", 
            size=75, 
            is_hashable=True, 
            in_memory=True,
            content=b"content2",
        ),
    ]

@pytest.mark.asyncio
async def test_test_file_model_save_and_retrieve(initialized_context, sample_file_stacks):
    """Test saving and retrieving SampleFileModel with embedded FileList"""
    # 1. Save FileStacks first (as they are referenced by FileList via ObjectIds)
    for fs in sample_file_stacks:
        await context.db.save(fs)
    
    # 2. Create FileList and add FileStacks
    file_list = FileList()
    file_list.extend(sample_file_stacks)
    
    # 3. Create SampleFileModel and embed FileList
    model = SampleFileModel(file_list=file_list, name="Test Model")
    
    # 4. Save SampleFileModel
    saved_model = await context.db.save(model)
    assert saved_model.id is not None
    assert len(saved_model.file_list) == 2
    
    # 5. Retrieve SampleFileModel from database
    retrieved_model = await context.db.find_one(SampleFileModel, SampleFileModel.id == saved_model.id)
    
    assert retrieved_model is not None
    assert retrieved_model.name == "Test Model"
    assert len(retrieved_model.file_list) == 2

    retrieved_files = list(retrieved_model.file_list)
    assert len(retrieved_files) == 2
    assert retrieved_files[0].name == "file1.txt"
    assert retrieved_files[1].name == "file2.txt"
    assert retrieved_files[0].content == b"content1"
    assert retrieved_files[1].content == b"content2"

@pytest.mark.asyncio
async def test_test_file_model_empty_list(initialized_context):
    """Test SampleFileModel with an empty FileList"""
    model = SampleFileModel(name="Empty Model")
    saved_model = await context.db.save(model)
    
    retrieved_model = await context.db.find_one(SampleFileModel, SampleFileModel.id == saved_model.id)
    assert retrieved_model is not None
    assert len(retrieved_model.file_list) == 0
