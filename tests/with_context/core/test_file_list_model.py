import pytest
import tempfile
import os
from pathlib import Path
from simstack.models.files import FileStack
from simstack.models.file_list import FileList, FileListModel
from simstack.core.context import context


@pytest.fixture
def sample_file_stack():
    """Create a sample FileStack for testing"""
    return FileStack(
        name="test_file.txt",
        size=100,
        is_hashable=True,
        in_memory=True,
        content=b"compressed_test_content",
    )


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
        FileStack(name="file2.py", size=75, is_hashable=False, in_memory=False),
        FileStack(
            name="test_data.csv",
            size=200,
            is_hashable=True,
            in_memory=True,
            content=b"csv_content",
        ),
    ]


class TestFileListMixin:
    """Test the common functionality in FileListMixin"""

    def test_len_empty_list(self):
        """Test __len__ method with empty file list"""
        file_list = FileList()
        assert len(file_list) == 0

    @pytest.mark.asyncio
    async def test_len_with_files(self, sample_file_stacks):
        """Test __len__ method with files in list"""
        file_list = FileList()
        for file_stack in sample_file_stacks:
            await file_list.append(file_stack)
        assert len(file_list) == 3

    @pytest.mark.asyncio
    async def test_append_single_file(self, sample_file_stack):
        """Test appending a single file"""
        file_list = FileList()
        await file_list.append(sample_file_stack)

        assert len(file_list) == 1
        # In ObjectListMixin, file_list[0] returns the ObjectId, not the object
        assert file_list[0] == sample_file_stack.id

    @pytest.mark.asyncio
    async def test_append_multiple_files(self, sample_file_stacks):
        """Test appending multiple files"""
        file_list = FileList()
        for file_stack in sample_file_stacks:
            await file_list.append(file_stack)

        assert len(file_list) == 3
        # In ObjectListMixin, elements contains ObjectIds
        assert file_list.elements == [fs.id for fs in sample_file_stacks]

    @pytest.mark.asyncio
    async def test_find_existing_pattern(self, sample_file_stacks):
        """Test finding file with existing pattern"""
        file_list = FileList()
        for file_stack in sample_file_stacks:
            await file_list.append(file_stack)

        # Find .py file
        result = await file_list.find("file2.py")
        assert result is not None
        assert result.name == "file2.py"

    @pytest.mark.asyncio
    async def test_find_non_existing_pattern(self, sample_file_stacks):
        """Test finding file with non-existing pattern"""
        file_list = FileList()
        for file_stack in sample_file_stacks:
            await file_list.append(file_stack)

        # Try to find .pdf file (doesn't exist)
        result = await file_list.find(r"\.pdf$")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_case_sensitive(self, sample_file_stacks):
        """Test case-sensitive pattern matching"""
        file_list = FileList()
        for file_stack in sample_file_stacks:
            await file_list.append(file_stack)

        # Find files starting with "test" (case sensitive)
        result = await file_list.find("test_data.csv")
        assert result is not None
        assert result.name == "test_data.csv"

    @pytest.mark.asyncio
    async def test_find_all_matching_pattern(self, sample_file_stacks):
        """Test finding all files matching pattern"""
        file_list = FileList()
        for file_stack in sample_file_stacks:
            await file_list.append(file_stack)

        # Find all .txt files
        results = await file_list.find_all(r"\.txt$")
        assert len(results) == 1
        assert results[0].name == "file1.txt"

    @pytest.mark.asyncio
    async def test_find_all_multiple_matches(self, sample_file_stacks):
        """Test finding all files when multiple match"""
        file_list = FileList()
        for file_stack in sample_file_stacks:
            await file_list.append(file_stack)

        # Add another .txt file
        extra_file = FileStack(name="another.txt", size=25)
        await file_list.append(extra_file)

        # Find all files containing "file" in name
        results = await file_list.find_all(r"file")
        assert len(results) == 2
        names = [f.name for f in results]
        assert "file1.txt" in names
        assert "file2.py" in names

    @pytest.mark.asyncio
    async def test_find_all_no_matches(self, sample_file_stacks):
        """Test finding all files when none match"""
        file_list = FileList()
        for file_stack in sample_file_stacks:
            await file_list.append(file_stack)

        # Try to find .exe files (none exist)
        results = await file_list.find_all(r"\.exe$")
        assert len(results) == 0
        assert results == []


class TestFileList:
    """Test FileList (EmbeddedModel) specific functionality"""

    def test_file_list_creation(self):
        """Test creating an empty FileList"""
        file_list = FileList()
        assert file_list.elements == []
        assert len(file_list) == 0


    @pytest.mark.asyncio
    async def test_file_list_initialization_with_data(self, initialized_context, sample_file_stacks):
        """Test creating FileList with initial data"""
        file_list = FileList()
        await file_list.extend(sample_file_stacks)
        assert len(file_list) == 3
        assert file_list.elements == [fs.id for fs in sample_file_stacks]


class TestFileListModel:
    """Test FileListModel (Model) specific functionality including database operations"""

    def test_file_list_model_creation(self):
        """Test creating an empty FileListModel"""
        file_list_model = FileListModel()
        assert len(file_list_model) == 0
        assert file_list_model.id is not None  # Model should have an ID

    @pytest.mark.asyncio
    async def test_file_list_model_initialization_with_data(self, initialized_context, sample_file_stacks):
        """Test creating FileListModel with initial data"""
        for fs in sample_file_stacks:
            await context.db.save(fs)

        file_list_model = FileListModel(elements=[fs.id for fs in sample_file_stacks])
        assert len(file_list_model) == 3
        loaded_stacks = []
        async for fs in file_list_model:
            loaded_stacks.append(fs)
        assert [fs.id for fs in loaded_stacks] == [fs.id for fs in sample_file_stacks]
        assert file_list_model.id is not None

    @pytest.mark.asyncio
    async def test_save_empty_file_list_model(self, initialized_context):
        """Test saving empty FileListModel to database"""
        file_list_model = FileListModel()

        # Save to database
        saved_model = await context.db.save(file_list_model)

        assert saved_model.id is not None
        assert len(saved_model) == 0
        assert saved_model.elements == []

    @pytest.mark.asyncio
    async def test_save_and_load_file_list_model(
        self, initialized_context, sample_file_stacks
    ):
        """Test saving and loading FileListModel with data"""
        # MUST save FileStacks first because they are References now
        for fs in sample_file_stacks:
            await context.db.save(fs)

        # Create and save FileListModel
        file_list_model = FileListModel(elements=[fs.id for fs in sample_file_stacks])
        saved_model = await context.db.save(file_list_model)

        # Load from database
        loaded_model = await context.db.find_one(
            FileListModel, FileListModel.id == saved_model.id
        )

        assert loaded_model is not None
        assert loaded_model.id == saved_model.id
        assert len(loaded_model) == 3

        # Check that file stack properties are preserved
        loaded_names = []
        async for fs in loaded_model:
            loaded_names.append(fs.name)
        original_names = [fs.name for fs in sample_file_stacks]
        assert loaded_names == original_names

    @pytest.mark.asyncio
    async def test_update_file_list_model_in_database(
        self, initialized_context, sample_file_stack
    ):
        """Test updating FileListModel in database"""
        await context.db.save(sample_file_stack)

        # Create and save initial model
        file_list_model = FileListModel()
        saved_model = await context.db.save(file_list_model)
        assert len(saved_model) == 0

        # Add file and update
        await saved_model.append(sample_file_stack)
        updated_model = await context.db.save(saved_model)

        # Verify update
        assert len(updated_model) == 1
        assert (await updated_model.get(0)).name == sample_file_stack.name

        # Load from database to confirm persistence
        loaded_model = await context.db.find_one(
            FileListModel, FileListModel.id == updated_model.id
        )
        assert len(loaded_model) == 1
        assert (await loaded_model.get(0)).name == sample_file_stack.name

    @pytest.mark.asyncio
    async def test_delete_file_list_model(
        self, initialized_context, sample_file_stacks
    ):
        """Test deleting FileListModel from database"""
        for fs in sample_file_stacks:
            await context.db.save(fs)

        # Create and save model
        file_list_model = FileListModel(elements=[fs.id for fs in sample_file_stacks])
        saved_model = await context.db.save(file_list_model)
        saved_id = saved_model.id

        # Verify it exists
        loaded_model = await context.db.find_one(
            FileListModel, FileListModel.id == saved_id
        )
        assert loaded_model is not None

        # Delete it
        await context.db.delete(saved_model)

        # Verify it's gone
        deleted_model = await context.db.find_one(
            FileListModel, FileListModel.id == saved_id
        )
        assert deleted_model is None

    @pytest.mark.asyncio
    async def test_find_multiple_file_list_models(
        self, initialized_context, sample_file_stacks
    ):
        """Test finding multiple FileListModels in database"""
        for fs in sample_file_stacks:
            await context.db.save(fs)

        # Create and save multiple models
        model1 = FileListModel(elements=[sample_file_stacks[0].id])
        model2 = FileListModel(elements=[fs.id for fs in sample_file_stacks[1:]])

        await context.db.save(model1)
        await context.db.save(model2)

        # Find all FileListModels
        all_models = await context.db.find(FileListModel)

        # Should have at least our 2 models (may have more from other tests)
        assert len(all_models) >= 2

        # Check that our models are in the results
        model_ids = [m.id for m in all_models]
        assert model1.id in model_ids
        assert model2.id in model_ids

    @pytest.mark.asyncio
    async def test_file_list_model_with_file_operations(self, initialized_context):
        """Test FileListModel with actual file operations"""
        # Create a temporary file
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".txt"
        ) as tmp_file:
            tmp_file.write("Test file content for FileListModel")
            tmp_file_path = tmp_file.name

        try:
            # Create FileStack from the temporary file
            file_stack = FileStack.from_local_file(
                path=tmp_file_path, is_hashable=True, in_memory=True
            )
            await context.db.save(file_stack)

            # Create FileListModel and add the file stack
            file_list_model = FileListModel()
            await file_list_model.append(file_stack)

            # Save to database
            saved_model = await context.db.save(file_list_model)

            # Verify save
            assert len(saved_model) == 1
            fs0 = await saved_model.get(0)
            assert fs0.name == Path(tmp_file_path).name
            assert fs0.in_memory is True

            # Load from database
            loaded_model = await context.db.find_one(
                FileListModel, FileListModel.id == saved_model.id
            )

            # Verify load
            assert loaded_model is not None
            assert len(loaded_model) == 1
            fs0_loaded = await loaded_model.get(0)
            assert fs0_loaded.name == Path(tmp_file_path).name

        finally:
            # Clean up temporary file
            if os.path.exists(tmp_file_path):
                os.unlink(tmp_file_path)


class TestFileListModelIntegration:
    """Integration tests for FileListModel with complex scenarios"""

    @pytest.mark.asyncio
    async def test_large_file_list_model_operations(self, initialized_context):
        """Test FileListModel with many files"""
        # Create many file stacks
        file_stacks = []
        for i in range(50):
            file_stack = FileStack(
                name=f"file_{i:03d}.txt",
                size=i * 10,
                is_hashable=i % 2 == 0,  # Every other file is hashable
                in_memory=i % 3 == 0,  # Every third file is in memory
            )
            await context.db.save(file_stack)
            file_stacks.append(file_stack)

        # Create and save model
        file_list_model = FileListModel(elements=[fs.id for fs in file_stacks])
        saved_model = await context.db.save(file_list_model)

        # Verify save
        assert len(saved_model) == 50

        # Test find operations
        txt_files = await saved_model.find_all(r"file_.*\.txt$")
        assert len(txt_files) == 50

        # Test finding specific files
        file_010 = await saved_model.find("file_010.txt")
        assert file_010 is not None
        assert file_010.name == "file_010.txt"

        # Load from database and verify
        loaded_model = await context.db.find_one(
            FileListModel, FileListModel.id == saved_model.id
        )

        assert len(loaded_model) == 50
        assert len(await loaded_model.find_all(r"file_.*\.txt$")) == 50


# Additional utility functions for testing
def create_test_file_stack(
    name: str, size: int = 100, in_memory: bool = True
) -> FileStack:
    """Utility function to create test FileStack objects"""
    return FileStack(
        name=name,
        size=size,
        is_hashable=True,
        in_memory=in_memory,
        content=b"test_content" if in_memory else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("file_count", [0, 1, 5, 10])
async def test_file_list_len_parametrized(initialized_context, file_count):
    """Parametrized test for len() method with different file counts"""
    file_list = FileList()

    # Add specified number of files
    for i in range(file_count):
        file_stack = create_test_file_stack(f"file_{i}.txt")
        await file_list.append(file_stack)

    assert len(file_list) == file_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern,expected_count",
    [(r"file1\.txt$", 1), (r"file2\.py$", 1), (r"^test", 1), (r"file", 2), (r"\.exe$", 0)],
)
async def test_find_all_patterns(initialized_context, pattern, expected_count, sample_file_stacks):
    """Parametrized test for find_all with different patterns"""
    file_list = FileList()
    for fs in sample_file_stacks:
        await file_list.append(fs)
    results = await file_list.find_all(pattern)
    assert len(results) == expected_count
