import pytest
from datetime import datetime
from uuid import uuid4

# Import the required modules
from simstack.core.context import context
from simstack.models.dataset_metadata import DataSetMetadata, DataSetMetadataTemplate


class TestDataSetMetadataBasic:
    """Test basic functionality and initialization."""

    def test_empty_initialization(self):
        """Test creating an empty DataSetMetadata."""
        metadata = DataSetMetadata(field_name="test_empty")
        assert metadata.field_name == "test_empty"
        assert metadata.data == {}
        assert metadata.initialized is False
        assert len(metadata) == 0

    def test_initialization_with_data(self):
        """Test creating DataSetMetadata with initial data."""
        initial_data = {
            "name": "test_dataset",
            "count": 42,
            "active": True,
            "timestamp": datetime(2023, 1, 1),
        }
        metadata = DataSetMetadata(field_name="experiment_basic", data=initial_data)

        assert metadata.field_name == "experiment_basic"
        assert metadata.data == initial_data
        assert metadata.initialized is False
        assert len(metadata) == 4


class TestDataSetMetadataDictBehavior:
    """Test dict-like behavior."""

    @pytest.fixture
    def metadata_with_data(self):
        """Fixture providing metadata with sample data."""
        return DataSetMetadata(
            field_name="test_dict_behavior",
            data={
                "name": "sample",
                "count": 10,
                "active": True,
                "created": datetime(2023, 1, 1, 12, 0, 0),
            },
        )

    def test_getitem(self, metadata_with_data):
        """Test getting items using [] notation."""
        assert metadata_with_data["name"] == "sample"
        assert metadata_with_data["count"] == 10
        assert metadata_with_data["active"] is True
        assert metadata_with_data["created"] == datetime(2023, 1, 1, 12, 0, 0)

    def test_getitem_keyerror(self, metadata_with_data):
        """Test KeyError when accessing non-existent key."""
        with pytest.raises(KeyError):
            _ = metadata_with_data["nonexistent"]

    def test_setitem_existing_key_same_type(self, metadata_with_data):
        """Test updating existing key with same type."""
        metadata_with_data["name"] = "new_name"
        assert metadata_with_data["name"] == "new_name"

        metadata_with_data["count"] = 20
        assert metadata_with_data["count"] == 20

        metadata_with_data["active"] = False
        assert metadata_with_data["active"] is False

    def test_setitem_new_key_before_initialization_succeeds(self, metadata_with_data):
        """Test that adding new keys succeeds while metadata is not initialized."""
        metadata_with_data["new_key"] = "value"
        assert metadata_with_data["new_key"] == "value"

    def test_setitem_type_change_before_initialization_succeeds(
        self, metadata_with_data
    ):
        """Test that changing existing value types succeeds before initialization."""
        metadata_with_data["count"] = "not_a_number"
        assert metadata_with_data["count"] == "not_a_number"

        metadata_with_data["active"] = 1
        assert metadata_with_data["active"] == 1

    def test_setitem_invalid_type_fails(self, metadata_with_data):
        """Test that invalid value types fail."""
        with pytest.raises(
            TypeError, match="Value must be str, int, float, bool, or datetime"
        ):
            metadata_with_data["name"] = ["invalid", "list"]

    def test_contains(self, metadata_with_data):
        """Test 'in' operator."""
        assert "name" in metadata_with_data
        assert "count" in metadata_with_data
        assert "nonexistent" not in metadata_with_data

    def test_len(self, metadata_with_data):
        """Test len() function."""
        assert len(metadata_with_data) == 4

    def test_iter(self, metadata_with_data):
        """Test iteration over keys."""
        keys = list(metadata_with_data)
        expected_keys = ["name", "count", "active", "created"]
        assert set(keys) == set(expected_keys)

    def test_keys_values_items(self, metadata_with_data):
        """Test keys(), values(), and items() methods."""
        keys = list(metadata_with_data.keys())
        values = list(metadata_with_data.values())
        items = dict(metadata_with_data.items())

        expected_keys = ["name", "count", "active", "created"]
        expected_values = ["sample", 10, True, datetime(2023, 1, 1, 12, 0, 0)]
        expected_items = {
            "name": "sample",
            "count": 10,
            "active": True,
            "created": datetime(2023, 1, 1, 12, 0, 0),
        }

        assert set(keys) == set(expected_keys)
        assert set(values) == set(expected_values)
        assert items == expected_items

    def test_get(self, metadata_with_data):
        """Test get() method."""
        assert metadata_with_data.get("name") == "sample"
        assert metadata_with_data.get("nonexistent") is None
        assert metadata_with_data.get("nonexistent", "default") == "default"


class TestDataSetMetadataRestrictions:
    """Test restrictions after initialization."""

    @pytest.fixture
    def metadata_with_data(self):
        return DataSetMetadata(
            field_name="test_restrictions", data={"name": "sample", "count": 10}
        )

    def test_delitem_before_initialization_succeeds(self, metadata_with_data):
        """Test that deleting keys succeeds while metadata is not initialized."""
        del metadata_with_data["name"]
        assert "name" not in metadata_with_data

    def test_pop_before_initialization_succeeds(self, metadata_with_data):
        """Test that pop() succeeds while metadata is not initialized."""
        assert metadata_with_data.pop("name") == "sample"
        assert "name" not in metadata_with_data

    def test_popitem_before_initialization_succeeds(self, metadata_with_data):
        """Test that popitem() succeeds while metadata is not initialized."""
        key, value = metadata_with_data.popitem()
        assert (key, value) == ("count", 10)
        assert len(metadata_with_data) == 1

    def test_clear_before_initialization_succeeds(self, metadata_with_data):
        """Test that clear() succeeds while metadata is not initialized."""
        metadata_with_data.clear()
        assert metadata_with_data.data == {}

    def test_setdefault_new_key_before_initialization_succeeds(
        self, metadata_with_data
    ):
        """Test that setdefault() with new key succeeds before initialization."""
        result = metadata_with_data.setdefault("new_key", "default")
        assert result == "default"
        assert metadata_with_data["new_key"] == "default"

    def test_setdefault_existing_key_works(self, metadata_with_data):
        """Test that setdefault() with existing key works."""
        result = metadata_with_data.setdefault("name", "default")
        assert result == "sample"  # Should return existing value
        assert metadata_with_data["name"] == "sample"  # Should not change


class TestDataSetMetadataUpdate:
    """Test update() method behavior."""

    @pytest.fixture
    def metadata_with_data(self):
        return DataSetMetadata(
            field_name="test_update",
            data={"name": "sample", "count": 10, "active": True},
        )

    def test_update_existing_keys_same_types(self, metadata_with_data):
        """Test updating existing keys with compatible types."""
        metadata_with_data.update({"name": "updated", "count": 20})
        assert metadata_with_data["name"] == "updated"
        assert metadata_with_data["count"] == 20

    def test_update_with_kwargs(self, metadata_with_data):
        """Test updating using keyword arguments."""
        metadata_with_data.update(name="updated", count=20)
        assert metadata_with_data["name"] == "updated"
        assert metadata_with_data["count"] == 20

    def test_update_new_key_before_initialization_succeeds(self, metadata_with_data):
        """Test that update() with new keys succeeds before initialization."""
        metadata_with_data.update({"new_key": "value"})
        assert metadata_with_data["new_key"] == "value"

    def test_update_type_mismatch_before_initialization_succeeds(
        self, metadata_with_data
    ):
        """Test that update() with type mismatches succeeds before initialization."""
        metadata_with_data.update({"count": "not_a_number"})
        assert metadata_with_data["count"] == "not_a_number"

    def test_update_invalid_type_fails(self, metadata_with_data):
        """Test that update() with invalid types fails."""
        with pytest.raises(
            TypeError,
            match="Value for key 'name' must be str, int, float, bool, or datetime",
        ):
            metadata_with_data.update({"name": ["invalid", "list"]})

    def test_update_multiple_keys_before_initialization_succeeds(
        self, metadata_with_data
    ):
        """Test that update() can change existing keys and add new keys before initialization."""
        metadata_with_data.update(
            {
                "name": "updated",
                "count": 20,
                "new_key": "added",
            }
        )

        assert metadata_with_data["name"] == "updated"
        assert metadata_with_data["count"] == 20
        assert metadata_with_data["new_key"] == "added"


class TestDataSetMetadataUtilityMethods:
    """Test utility methods."""

    @pytest.fixture
    def metadata_with_data(self):
        return DataSetMetadata(
            field_name="test_utility",
            data={
                "name": "sample",
                "count": 10,
                "price": 99.99,
                "active": True,
                "created": datetime(2023, 1, 1),
            },
        )

    def test_copy_data(self, metadata_with_data):
        """Test copy_data() method."""
        copied = metadata_with_data.copy_data()
        assert copied == metadata_with_data.data
        assert copied is not metadata_with_data.data  # Should be a copy

        # Modifying copy shouldn't affect original
        copied["name"] = "modified"
        assert metadata_with_data["name"] == "sample"

    def test_is_type_compatible_existing_key(self, metadata_with_data):
        """Test is_type_compatible() with existing keys."""
        assert metadata_with_data.is_type_compatible("name", "other_string")
        assert metadata_with_data.is_type_compatible("count", 42)
        assert metadata_with_data.is_type_compatible("price", 12.34)
        assert metadata_with_data.is_type_compatible("active", False)
        assert metadata_with_data.is_type_compatible("created", datetime.now())

        # Wrong types
        assert not metadata_with_data.is_type_compatible("count", "string")
        assert not metadata_with_data.is_type_compatible("active", 1)

    def test_is_type_compatible_new_key(self, metadata_with_data):
        """Test is_type_compatible() with new keys."""
        assert metadata_with_data.is_type_compatible("new_key", "string")
        assert metadata_with_data.is_type_compatible("new_key", 42)
        assert metadata_with_data.is_type_compatible("new_key", 3.14)
        assert metadata_with_data.is_type_compatible("new_key", True)
        assert metadata_with_data.is_type_compatible("new_key", datetime.now())

        # Invalid types
        assert not metadata_with_data.is_type_compatible("new_key", ["list"])
        assert not metadata_with_data.is_type_compatible("new_key", {"dict": "value"})

    def test_get_key_type(self, metadata_with_data):
        """Test get_key_type() method."""
        assert metadata_with_data.get_key_type("name") == str
        assert metadata_with_data.get_key_type("count") == int
        assert metadata_with_data.get_key_type("price") == float
        assert metadata_with_data.get_key_type("active") == bool
        assert metadata_with_data.get_key_type("created") == datetime

    def test_get_key_type_nonexistent(self, metadata_with_data):
        """Test get_key_type() with nonexistent key."""
        with pytest.raises(KeyError, match="Key 'nonexistent' not found"):
            metadata_with_data.get_key_type("nonexistent")

    def test_get_schema_for_key(self, metadata_with_data):
        """Test get_schema_for_key() method."""
        assert metadata_with_data.get_schema_for_key("name") == {"type": "string"}
        assert metadata_with_data.get_schema_for_key("count") == {"type": "integer"}
        assert metadata_with_data.get_schema_for_key("price") == {"type": "number"}
        assert metadata_with_data.get_schema_for_key("active") == {"type": "boolean"}
        assert metadata_with_data.get_schema_for_key("created") == {
            "type": "string",
            "format": "date-time",
        }

    def test_get_schema_for_key_nonexistent(self, metadata_with_data):
        """Test get_schema_for_key() with nonexistent key."""
        with pytest.raises(KeyError, match="Key 'nonexistent' not found"):
            metadata_with_data.get_schema_for_key("nonexistent")


class TestDataSetMetadataJsonSchema:
    """Test JSON schema generation."""

    def test_get_data_json_schema_empty(self):
        """Test JSON schema for empty data."""
        metadata = DataSetMetadata(field_name="test_json_schema_empty")
        schema = metadata.get_json_schema()

        expected = {"type": "object", "properties": {}, "additionalProperties": False}
        assert schema == expected

    def test_get_data_json_schema_with_data(self):
        """Test JSON schema with various data types."""
        metadata = DataSetMetadata(
            field_name="test_json_schema_full",
            data={
                "name": "sample",
                "count": 10,
                "price": 99.99,
                "active": True,
                "created": datetime(2023, 1, 1),
            },
        )

        schema = metadata.get_json_schema()

        expected = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "price": {"type": "number"},
                "active": {"type": "boolean"},
                "created": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": False,
        }
        assert schema == expected


class TestDataSetMetadataStructureValidation:
    """Test metadata structure validation and same-name restrictions."""

    @pytest.mark.asyncio
    async def test_first_validation_initializes_structure_for_freeze(
        self, initialized_context
    ):
        """The first validated structure is immediately available to freeze."""
        field_name = f"first-validation-{uuid4()}"
        structure = {"train": {"value": "FloatData"}}
        metadata = DataSetMetadata(field_name=field_name, data={"label": "first"})

        try:
            assert await metadata.validate_dict(structure) is True
            assert metadata.structure == structure
            assert metadata.freeze(structure) is True
        finally:
            template = await context.db.find_one(
                DataSetMetadataTemplate,
                DataSetMetadataTemplate.dataset_type == field_name,
            )
            if template is not None:
                await context.db.delete(template)

    @pytest.mark.asyncio
    async def test_new_section_preserves_existing_template_structure(
        self, initialized_context
    ):
        """Adding a section extends rather than replaces the stored template."""
        field_name = f"additive-section-{uuid4()}"
        train = {"train": {"value": "FloatData"}}
        validation = {"validation": {"value": "FloatData"}}
        expected = {**train, **validation}
        first = DataSetMetadata(field_name=field_name, data={"label": "first"})
        second = DataSetMetadata(field_name=field_name, data={"label": "second"})

        try:
            assert await first.validate_dict(train) is True
            assert await second.validate_dict(validation) is True

            template = await context.db.find_one(
                DataSetMetadataTemplate,
                DataSetMetadataTemplate.dataset_type == field_name,
            )
            assert template is not None
            assert template.structure == expected
            assert second.structure == expected
        finally:
            template = await context.db.find_one(
                DataSetMetadataTemplate,
                DataSetMetadataTemplate.dataset_type == field_name,
            )
            if template is not None:
                await context.db.delete(template)

    @pytest.mark.asyncio
    async def test_same_structure_different_names_succeed(self):
        """Test that metadata with same structures but different names succeed."""
        # First metadata with structure: name (str), count (int), active (bool)
        metadata1 = DataSetMetadata(
            field_name="analysis_type_1",
            data={"name": "analysis_1", "count": 100, "active": True},
        )

        # Second metadata with SAME structure but different dataset_type name
        metadata2 = DataSetMetadata(
            field_name="analysis_type_2",
            data={"name": "analysis_2", "count": 200, "active": False},
        )

        assert metadata1.field_name == "analysis_type_1"
        assert metadata2.field_name == "analysis_type_2"

    @pytest.mark.asyncio
    async def test_same_structure_same_name_succeeds(self):
        """Test that metadata with same structure and same name succeeds."""

        # First metadata
        DataSetMetadata(
            field_name="valid_same_structure",
            data={"name": "test_1", "version": 1, "stable": True},
        )

        # Second metadata with same structure and same dataset_type name should succeed
        metadata2 = DataSetMetadata(
            field_name="valid_same_structure",  # Same name as first
            data={
                "name": "test_2",  # Same structure: name (str)
                "version": 2,  # Same structure: version (int)
                "stable": False,  # Same structure: stable (bool)
            },
        )
        # This should succeed

        assert metadata2.field_name == "valid_same_structure"


class TestDataSetMetadataEdgeCases:
    """Test edge cases and error conditions."""

    def test_bool_vs_int_distinction(self):
        """Test that bool and int are treated as different types."""
        metadata = DataSetMetadata(field_name="test_bool_int", data={"flag": True})

        metadata["flag"] = 1
        assert metadata["flag"] == 1

        metadata2 = DataSetMetadata(field_name="test_int_bool", data={"number": 42})
        metadata2["number"] = True
        assert metadata2["number"] is True

    def test_float_vs_int_distinction(self):
        """Test that float and int are treated as different types."""
        metadata = DataSetMetadata(field_name="test_float_int", data={"number": 42})

        metadata["number"] = 42.0
        assert metadata["number"] == 42.0

    def test_empty_string_handling(self):
        """Test handling of empty strings."""
        metadata = DataSetMetadata(field_name="test_empty_string", data={"name": ""})
        assert metadata["name"] == ""

        # Should still be able to update with another string
        metadata["name"] = "updated"
        assert metadata["name"] == "updated"

    def test_datetime_handling(self):
        """Test datetime handling specifics."""
        dt = datetime(2023, 1, 1, 12, 30, 45)
        metadata = DataSetMetadata(field_name="test_datetime", data={"timestamp": dt})

        assert metadata["timestamp"] == dt

        # Should be able to update with another datetime
        new_dt = datetime(2023, 12, 31, 23, 59, 59)
        metadata["timestamp"] = new_dt
        assert metadata["timestamp"] == new_dt


# Run the tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
