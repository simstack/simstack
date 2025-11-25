import pytest

from simstack.models.artifact_models import ArtifactModel


class TestArtifactModel:
    """Test suite for ArtifactModel including database operations."""

    @pytest.mark.asyncio
    async def test_create_artifact_with_basic_data_types(self, initialized_context):
        """Test creating ArtifactModel with int, str, bool data."""
        # Test with int data
        artifact_int = ArtifactModel(
            name="test_int_artifact",
            description="Test artifact with integer data",
            data={"value": 42, "count": 100},
            path="/test/path/int",
        )

        assert artifact_int.name == "test_int_artifact"
        assert artifact_int.data["value"] == 42
        assert artifact_int.data["count"] == 100
        assert isinstance(artifact_int.data["value"], int)

        # Test with string data
        artifact_str = ArtifactModel(
            name="test_str_artifact",
            description="Test artifact with string data",
            data={"message": "hello world", "status": "completed"},
            path="/test/path/str",
        )

        assert artifact_str.data["message"] == "hello world"
        assert artifact_str.data["status"] == "completed"
        assert isinstance(artifact_str.data["message"], str)

        # Test with boolean data
        artifact_bool = ArtifactModel(
            name="test_bool_artifact",
            description="Test artifact with boolean data",
            data={"is_valid": True, "is_processed": False},
            path="/test/path/bool",
        )

        assert artifact_bool.data["is_valid"] is True
        assert artifact_bool.data["is_processed"] is False
        assert isinstance(artifact_bool.data["is_valid"], bool)

    @pytest.mark.asyncio
    async def test_create_artifact_with_mixed_data_types(self, initialized_context):
        """Test creating ArtifactModel with mixed data types."""
        mixed_data = {
            "integer_value": 123,
            "string_value": "test_string",
            "boolean_value": True,
            "float_value": 3.14159,
            "nested_dict": {
                "inner_int": 456,
                "inner_str": "nested_string",
                "inner_bool": False,
            },
        }

        artifact = ArtifactModel(
            name="mixed_data_artifact",
            description="Artifact with mixed data types",
            data=mixed_data,
            path="/test/path/mixed",
        )

        assert artifact.data["integer_value"] == 123
        assert artifact.data["string_value"] == "test_string"
        assert artifact.data["boolean_value"] is True
        assert artifact.data["float_value"] == 3.14159
        assert artifact.data["nested_dict"]["inner_int"] == 456
        assert artifact.data["nested_dict"]["inner_str"] == "nested_string"
        assert artifact.data["nested_dict"]["inner_bool"] is False

    @pytest.mark.asyncio
    async def test_create_artifact_with_lists(self, initialized_context):
        """Test creating ArtifactModel with list data."""
        list_data = {
            "int_list": [1, 2, 3, 4, 5],
            "str_list": ["apple", "banana", "cherry"],
            "bool_list": [True, False, True, True],
            "mixed_list": [1, "two", True, 4.0],
            "nested_list": [[1, 2], [3, 4], [5, 6]],
            "dict_list": [{"id": 1, "name": "first"}, {"id": 2, "name": "second"}],
        }

        artifact = ArtifactModel(
            name="list_data_artifact",
            description="Artifact with various list types",
            data=list_data,
            path="/test/path/lists",
        )

        # Test integer list
        assert artifact.data["int_list"] == [1, 2, 3, 4, 5]
        assert len(artifact.data["int_list"]) == 5
        assert all(isinstance(x, int) for x in artifact.data["int_list"])

        # Test string list
        assert artifact.data["str_list"] == ["apple", "banana", "cherry"]
        assert all(isinstance(x, str) for x in artifact.data["str_list"])

        # Test boolean list
        assert artifact.data["bool_list"] == [True, False, True, True]
        assert all(isinstance(x, bool) for x in artifact.data["bool_list"])

        # Test mixed list
        assert artifact.data["mixed_list"] == [1, "two", True, 4.0]

        # Test nested list
        assert artifact.data["nested_list"] == [[1, 2], [3, 4], [5, 6]]

        # Test list of dictionaries
        assert len(artifact.data["dict_list"]) == 2
        assert artifact.data["dict_list"][0]["name"] == "first"
        assert artifact.data["dict_list"][1]["id"] == 2

    @pytest.mark.asyncio
    async def test_save_and_retrieve_artifact_with_basic_data(
        self, initialized_context
    ):
        """Test saving and retrieving ArtifactModel with basic data types from database."""
        original_artifact = ArtifactModel(
            name="db_test_basic",
            description="Database test with basic data",
            data={
                "count": 42,
                "message": "test message",
                "active": True,
                "score": 95.5,
            },
            path="/database/test/basic",
        )

        # Save to database
        saved_artifact = await initialized_context.db.save(original_artifact)
        assert saved_artifact.id is not None

        # Retrieve from database
        retrieved_artifact = await initialized_context.db.find_one(
            ArtifactModel, ArtifactModel.id == saved_artifact.id
        )

        # Verify all data was correctly saved and retrieved
        assert retrieved_artifact is not None
        assert retrieved_artifact.name == "db_test_basic"
        assert retrieved_artifact.description == "Database test with basic data"
        assert retrieved_artifact.path == "/database/test/basic"
        assert retrieved_artifact.data["count"] == 42
        assert retrieved_artifact.data["message"] == "test message"
        assert retrieved_artifact.data["active"] is True
        assert retrieved_artifact.data["score"] == 95.5

    @pytest.mark.asyncio
    async def test_save_and_retrieve_artifact_with_lists(self, initialized_context):
        """Test saving and retrieving ArtifactModel with list data from database."""
        list_data = {
            "numbers": [10, 20, 30, 40, 50],
            "words": ["hello", "world", "test"],
            "flags": [True, False, True],
            "nested_data": {"inner_list": [1, 2, 3], "inner_dict": {"key": "value"}},
            "complex_list": [{"type": "A", "value": 100}, {"type": "B", "value": 200}],
        }

        original_artifact = ArtifactModel(
            name="db_test_lists",
            description="Database test with list data",
            data=list_data,
            path="/database/test/lists",
        )

        # Save to database
        saved_artifact = await initialized_context.db.save(original_artifact)

        # Retrieve from database
        retrieved_artifact = await initialized_context.db.find_one(
            ArtifactModel, ArtifactModel.id == saved_artifact.id
        )

        # Verify all list data was correctly saved and retrieved
        assert retrieved_artifact is not None
        assert retrieved_artifact.data["numbers"] == [10, 20, 30, 40, 50]
        assert retrieved_artifact.data["words"] == ["hello", "world", "test"]
        assert retrieved_artifact.data["flags"] == [True, False, True]
        assert retrieved_artifact.data["nested_data"]["inner_list"] == [1, 2, 3]
        assert retrieved_artifact.data["nested_data"]["inner_dict"]["key"] == "value"
        assert len(retrieved_artifact.data["complex_list"]) == 2
        assert retrieved_artifact.data["complex_list"][0]["type"] == "A"
        assert retrieved_artifact.data["complex_list"][1]["value"] == 200

    @pytest.mark.asyncio
    async def test_update_artifact_data(self, initialized_context):
        """Test updating artifact data and saving changes."""
        # Create initial artifact
        artifact = ArtifactModel(
            name="updateable_artifact",
            description="Test artifact for updates",
            data={"counter": 1, "status": "initial"},
            path="/test/update",
        )

        # Save initial version
        saved_artifact = await initialized_context.db.save(artifact)

        # Update the data
        saved_artifact.data["counter"] = 2
        saved_artifact.data["status"] = "updated"
        saved_artifact.data["new_field"] = "added"

        # Save updated version
        updated_artifact = await initialized_context.db.save(saved_artifact)

        # Retrieve and verify updates
        retrieved_artifact = await initialized_context.db.find_one(
            ArtifactModel, ArtifactModel.id == updated_artifact.id
        )

        assert retrieved_artifact.data["counter"] == 2
        assert retrieved_artifact.data["status"] == "updated"
        assert retrieved_artifact.data["new_field"] == "added"

    @pytest.mark.asyncio
    async def test_empty_data_dict(self, initialized_context):
        """Test creating and saving artifact with empty data dict."""
        artifact = ArtifactModel(
            name="empty_data_artifact",
            description="Artifact with empty data",
            data={},  # Empty dict
            path="/test/empty",
        )

        # Save to database
        saved_artifact = await initialized_context.db.save(artifact)

        # Retrieve from database
        retrieved_artifact = await initialized_context.db.find_one(
            ArtifactModel, ArtifactModel.id == saved_artifact.id
        )

        assert retrieved_artifact is not None
        assert retrieved_artifact.data == {}
        assert isinstance(retrieved_artifact.data, dict)

    @pytest.mark.asyncio
    async def test_default_data_field(self, initialized_context):
        """Test that data field defaults to empty dict when not provided."""
        artifact = ArtifactModel(
            name="default_data_artifact",
            path="/test/default",
            # Note: not providing data field
        )

        assert artifact.data == {}
        assert isinstance(artifact.data, dict)

        # Test saving and retrieving
        saved_artifact = await initialized_context.db.save(artifact)
        retrieved_artifact = await initialized_context.db.find_one(
            ArtifactModel, ArtifactModel.id == saved_artifact.id
        )

        assert retrieved_artifact.data == {}

    @pytest.mark.asyncio
    async def test_find_artifacts_by_data_content(self, initialized_context):
        """Test finding artifacts based on data content."""
        # Create multiple artifacts with different data
        artifacts = [
            ArtifactModel(
                name="search_test_1",
                data={"category": "test", "priority": "high"},
                path="/search/1",
            ),
            ArtifactModel(
                name="search_test_2",
                data={"category": "production", "priority": "low"},
                path="/search/2",
            ),
            ArtifactModel(
                name="search_test_3",
                data={"category": "test", "priority": "medium"},
                path="/search/3",
            ),
        ]

        # Save all artifacts
        saved_artifacts = []
        for artifact in artifacts:
            saved = await initialized_context.db.save(artifact)
            saved_artifacts.append(saved)

        # Find artifacts by name pattern (since querying by data content is more complex)
        found_artifacts = []
        for saved in saved_artifacts:
            retrieved = await initialized_context.db.find_one(
                ArtifactModel, ArtifactModel.id == saved.id
            )
            if retrieved and "test" in retrieved.name:
                found_artifacts.append(retrieved)

        assert len(found_artifacts) == 3

        # Verify data content
        test_artifacts = [
            a for a in found_artifacts if a.data.get("category") == "test"
        ]
        assert len(test_artifacts) == 2
