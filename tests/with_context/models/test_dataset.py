import pytest
from datetime import datetime
import uuid

from simstack.core.context import context
from simstack.models.dataset import DataSet, DataSetSection
from simstack.models.dataset_metadata import DataSetMetadata
from simstack.models import FloatData, StringData

class TestDataSetSection:
    """Test cases for DataSetSection functionality."""

    @pytest.mark.asyncio
    async def test_empty_section_initialization(self):
        """Test creating an empty DataSetSection."""
        section = DataSetSection()

        assert section.model_types == {}
        # assert section.data == {} # Removed data field
        assert len(section) == 0

    @pytest.mark.asyncio
    async def test_add_item(self):
        """Test adding a dictionary of models to a section."""
        # Create test models but don't save them yet
        float_data = FloatData(value=3.14)
        string_data = StringData(value="test")

        section = DataSetSection()
        item_name = "item1"
        section.add_row({"f": float_data, "s": string_data}, name=item_name)

        assert len(section) == 1
        assert section.model_types == {"f": "FloatData", "s": "StringData"}
        # assert section.data[item_name] == {"f": float_data.id, "s": string_data.id} # Removed data field
        
        # Verify it's in the cache
        cache = section._get_cache()
        assert item_name in cache
        assert cache[item_name]["f"] is float_data
        assert cache[item_name]["s"] is string_data

    @pytest.mark.asyncio
    async def test_add_item_auto_name(self):
        """Test adding an item without specifying a name."""
        float_data = FloatData(value=1.0)

        section = DataSetSection()
        section.add_row({"f": float_data})

        assert len(section) == 1
        name = list(section.keys())[0]
        # Should be a valid UUID
        uuid.UUID(name)

    @pytest.mark.asyncio
    async def test_add_item_type_mismatch_fails(self):
        """Test that adding models with mismatched types for the same key fails."""
        float_data = FloatData(value=1.0)
        string_data = StringData(value="test")

        section = DataSetSection()
        section.add_row({"f": float_data})

        # Should fail when adding a different type for key 'f'
        with pytest.raises(
            ValueError, match="Model type for key 'f' is StringData, but expected FloatData"
        ):
            section.add_row({"f": string_data})

    @pytest.mark.asyncio
    async def test_get_item(self):
        """Test retrieving items by name."""
        from simstack.core.context import context
        float_data = FloatData(value=42.0)
        await context.db.save(float_data)

        section = DataSetSection()
        section.add_row({"f": float_data}, name="test_item")

        from simstack.core.context import context
        retrieved = section.get_item("test_item")
        assert isinstance(retrieved["f"], FloatData)
        assert retrieved["f"].value == 42.0
        assert retrieved["f"].id == float_data.id

    @pytest.mark.asyncio
    async def test_get_item_key_error(self):
        """Test KeyError when accessing invalid item name."""
        section = DataSetSection()
        with pytest.raises(KeyError, match="Item with name 'nonexistent' not found"):
            await section.get_item("nonexistent")

class TestDataSet:
    """Test cases for DataSet functionality."""

    @pytest.mark.asyncio
    async def test_empty_dataset_initialization(self, initialized_context):
        """Test creating an empty DataSet."""
        from simstack.core.context import context
        metadata = DataSetMetadata(
            field_name="test_dataset",
            data={"description": "Empty test dataset"},
        )
        dataset = DataSet(metadata=metadata)
        await context.db.save(dataset)

        assert dataset.field_name == "dataset"
        assert len(dataset) == 0
        assert len(dataset.sections) == 0

    @pytest.mark.asyncio
    async def test_dataset_with_sections(self, initialized_context):
        """Test DataSet with multiple sections."""
        from simstack.core.context import context
        metadata = DataSetMetadata(
            field_name="test_multi_section",
            data={"description": "Multi-section test"},
        )
        
        float_data = FloatData(value=1.0)
        # Note: we don't save float_data here, DataSet.save() should do it

        section1 = DataSetSection()
        section1.add_row({"f": float_data}, name="s1_i1")

        dataset = DataSet(metadata=metadata)
        dataset["train"] = section1
        
        await dataset.save(context.db)
        
        assert len(dataset) == 1
        assert "train" in dataset
        assert len(dataset["train"]) == 1
        
        # Verify float_data is saved
        reloaded_float = await context.db.find_one(FloatData, FloatData.id == float_data.id)
        assert reloaded_float is not None
        assert reloaded_float.value == 1.0

    @pytest.mark.asyncio
    async def test_dataset_persistence(self, initialized_context):
        """Test saving and loading DataSet from the database."""
        from simstack.core.context import context
        metadata = DataSetMetadata(
            field_name="test_persistence",
            data={"version": "1.0"},
        )

        float_data = FloatData(value=123.45)

        section = DataSetSection()
        section.add_row({"f": float_data}, name="item1")

        dataset = DataSet(metadata=metadata)
        dataset["data"] = section
        await dataset.save(context.db)

        # Reload from DB
        loaded = await context.db.find_one(DataSet, DataSet.id == dataset.id)
        assert loaded is not None
        assert "data" in loaded
        assert len(loaded["data"]) == 1
        
        item = loaded["data"].get_item("item1")
        assert item["f"].value == 123.45


class TestDataSetSectionDict:
    """Test cases for dictionary-like functionality of DataSetSection."""

    @pytest.mark.asyncio
    async def test_getitem_setitem(self, initialized_context):
        section = DataSetSection()
        f1 = FloatData(value=1.0)

        # Test __setitem__
        section["item1"] = {"f": f1}
        assert "item1" in section
        assert len(section) == 1

        # Test __getitem__
        retrieved = section["item1"]
        assert retrieved["f"] is f1

        # Test overwrite
        f2 = FloatData(value=2.0)
        section["item1"] = {"f": f2}
        assert section["item1"]["f"] is f2
        assert len(section) == 1

    @pytest.mark.asyncio
    async def test_delitem(self, initialized_context):
        section = DataSetSection()
        f1 = FloatData(value=1.0)
        section["item1"] = {"f": f1}

        del section["item1"]
        assert "item1" not in section
        assert len(section) == 0

        with pytest.raises(KeyError):
            del section["nonexistent"]

    @pytest.mark.asyncio
    async def test_keys_values_items(self, initialized_context):
        section = DataSetSection()
        f1 = FloatData(value=1.0)
        f2 = FloatData(value=2.0)
        section["i1"] = {"f": f1}
        section["i2"] = {"f": f2}

        assert set(section.keys()) == {"i1", "i2"}
        assert len(section.values()) == 2

        items = dict(section.items())
        assert items["i1"]["f"] is f1
        assert items["i2"]["f"] is f2

    @pytest.mark.asyncio
    async def test_get_pop_clear(self, initialized_context):
        section = DataSetSection()
        f1 = FloatData(value=1.0)
        section["i1"] = {"f": f1}

        # get
        assert section.get("i1")["f"] is f1
        assert section.get("nonexistent", "default") == "default"

        # pop
        val = section.pop("i1")
        assert val["f"] is f1
        assert "i1" not in section

        with pytest.raises(KeyError):
            section.pop("nonexistent")
        assert section.pop("nonexistent", "default") == "default"

        # clear
        section["i2"] = {"f": f1}
        section.clear()
        assert len(section) == 0
        assert "i2" not in section

    @pytest.mark.asyncio
    async def test_update_popitem_setdefault(self, initialized_context):
        section = DataSetSection()
        f1 = FloatData(value=1.0)
        f2 = FloatData(value=2.0)

        # setdefault
        section.setdefault("i1", {"f": f1})
        assert section["i1"]["f"] is f1
        section.setdefault("i1", {"f": f2})
        assert section["i1"]["f"] is f1  # unchanged

        # popitem
        name, item = section.popitem()
        assert name == "i1"
        assert item["f"] is f1
        assert len(section) == 0

        # update from dict
        section.update({"i2": {"f": f2}})
        assert section["i2"]["f"] is f2

        # update from another DataSetSection
        other = DataSetSection()
        f3 = FloatData(value=3.0)
        other["i3"] = {"f": f3}
        section.update(other)
        assert section["i3"]["f"] is f3

    @pytest.mark.asyncio
    async def test_load_to_cache(self, initialized_context):
        f1 = FloatData(value=10.0)
        await context.db.save(f1)

        section = DataSetSection()
        # self.data is removed, so we add it to the section normally
        section.add_row({"f": f1}, name="item1")

        # Cache should NOT be empty because add_row populates it
        assert "item1" in section._get_cache()

        # Load to cache should now be a no-op
        await section.load_to_cache(context.db)

        assert "item1" in section._get_cache()
        assert section["item1"]["f"].id == f1.id
        assert section["item1"]["f"].value == 10.0
