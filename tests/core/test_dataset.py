import pytest
from datetime import datetime

from simstack.core.context import context
from simstack.core.engine import current_engine_context
from simstack.models.dataset import DataSet, DataSetSection
from simstack.models.dataset_metadata import DataSetMetadata
from simstack.models import FloatData, StringData
from simstack.models.node_registry import NodeRegistry


class TestDataSetSection:
    """Test cases for DataSetSection functionality."""

    @pytest.mark.asyncio
    async def test_empty_section_initialization(self):
        """Test creating an empty DataSetSection."""
        section = DataSetSection()

        assert section.model_types == []
        assert section.data == []
        assert len(section) == 0
        assert not bool(section)

    @pytest.mark.asyncio
    async def test_add_single_model_group(self):
        """Test adding a single tuple of models to a section."""
        # Create and save test models
        float_data = FloatData(value=3.14)
        string_data = StringData(value="test")
        await context.db.save(float_data)
        await context.db.save(string_data)

        section = DataSetSection()
        section.add_model_group((float_data, string_data))

        assert len(section) == 1
        assert section.model_types == ["FloatData", "StringData"]
        assert section.data[0] == [float_data.id, string_data.id]
        assert bool(section)

    @pytest.mark.asyncio
    async def test_add_multiple_model_groups_same_types(self):
        """Test adding multiple tuples with the same model types."""
        # Create and save test models
        float1 = FloatData(value=1.0)
        float2 = FloatData(value=2.0)
        string1 = StringData(value="first")
        string2 = StringData(value="second")

        for model in [float1, float2, string1, string2]:
            await context.db.save(model)

        section = DataSetSection()
        section.add_model_group((float1, string1))
        section.add_model_group((float2, string2))

        assert len(section) == 2
        assert section.model_types == ["FloatData", "StringData"]
        assert section.data[0] == [float1.id, string1.id]
        assert section.data[1] == [float2.id, string2.id]

    @pytest.mark.asyncio
    async def test_add_model_group_type_mismatch_fails(self):
        """Test that adding models with mismatched types fails."""
        float_data = FloatData(value=1.0)
        string_data = StringData(value="test")

        for model in [float_data, string_data]:
            await context.db.save(model)

        section = DataSetSection()
        section.add_model_group((float_data, string_data))

        # Should fail when adding different types
        with pytest.raises(
            ValueError, match="Model types .* don't match section's expected types"
        ):
            section.add_model_group((float_data, float_data))

    @pytest.mark.asyncio
    async def test_get_model_group(self):
        """Test retrieving model groups by index."""
        float_data = FloatData(value=42.0)
        string_data = StringData(value="answer")

        await context.db.save(float_data)
        await context.db.save(string_data)

        section = DataSetSection()
        section.add_model_group((float_data, string_data))

        retrieved = section.get_model_group(0)

        assert len(retrieved) == 2
        assert isinstance(retrieved[0], FloatData)
        assert isinstance(retrieved[1], StringData)
        assert retrieved[0].value == 42.0
        assert retrieved[1].value == "answer"

    @pytest.mark.asyncio
    async def test_get_model_group_index_error(self):
        """Test IndexError when accessing invalid index."""
        section = DataSetSection()

        with pytest.raises(IndexError, match="Index 0 out of range"):
            await section.get_model_group(0)

    @pytest.mark.asyncio
    async def test_get_all_tuples(self):
        """Test retrieving all tuples from a section."""
        float1 = FloatData(value=1.0)
        float2 = FloatData(value=2.0)
        string1 = StringData(value="one")
        string2 = StringData(value="two")

        for model in [float1, float2, string1, string2]:
            await context.db.save(model)

        section = DataSetSection()
        section.add_model_group((float1, string1))
        section.add_model_group((float2, string2))

        all_tuples = section.get_all_model_groups()

        assert len(all_tuples) == 2
        assert all_tuples[0][0].value == 1.0
        assert all_tuples[0][1].value == "one"
        assert all_tuples[1][0].value == 2.0
        assert all_tuples[1][1].value == "two"

    @pytest.mark.asyncio
    async def test_list_like_operations(self):
        """Test list-like operations on DataSetSection."""
        float1 = FloatData(value=10.0)
        float2 = FloatData(value=20.0)
        float3 = FloatData(value=30.0)
        string1 = StringData(value="ten")
        string2 = StringData(value="twenty")
        string3 = StringData(value="thirty")

        for model in [float1, float2, float3, string1, string2, string3]:
            await context.db.save(model)

        section = DataSetSection()

        # Test append
        section.append((float1, string1))
        section.append((float2, string2))
        assert len(section) == 2

        # Test insert
        section.insert(1, (float3, string3))
        assert len(section) == 3

        # Test __contains__
        assert (float1, string1) in section
        assert (float3, string3) in section

        # Test remove
        section.remove((float3, string3))
        assert len(section) == 2
        assert (float3, string3) not in section

        # Test clear
        section.clear()
        assert len(section) == 0
        assert section.model_types == []


class TestDataSet:
    """Test cases for DataSet functionality."""

    @pytest.mark.asyncio
    async def test_empty_dataset_initialization(self, real_database_context):
        """Test creating an empty DataSet."""
        metadata = DataSetMetadata(
            dataset_type="test_empty_with_description",
            data={"description": "Empty test dataset"},
        )

        dataset = DataSet(metadata=metadata)
        engine = current_engine_context.get()
        await dataset.save(engine)

        assert dataset.dataset_type == "test_empty_with_description"
        assert len(dataset) == 0
        assert len(dataset.sections) == 0

    @pytest.mark.asyncio
    async def test_dataset_with_sections(self, node_registry, real_database_context):
        """Test DataSet with multiple sections."""
        # Create metadata
        metadata = DataSetMetadata(
            dataset_type="test_multi_section",
            data={"description": "Multi-section test dataset"},
        )

        # Create test models
        float_data = FloatData(value=100.0)
        string_data = StringData(value="hundred")

        for model in [float_data, string_data]:
            await context.db.save(model)

        # Create sections
        section1 = DataSetSection()
        section1.add_model_group((float_data, string_data))

        node_registry_instance = node_registry
        section2 = DataSetSection()
        section2.add_model_group((node_registry_instance, float_data))

        # Create dataset
        dataset = DataSet(metadata=metadata)
        dataset["training"] = section1
        dataset["validation"] = section2

        engine = current_engine_context.get()
        await dataset.save(engine)

        assert len(dataset) == 2
        assert "training" in dataset
        assert "validation" in dataset

    @pytest.mark.asyncio
    async def test_dict_like_operations(self, real_database_context):
        """Test dictionary-like operations on DataSet."""
        metadata = DataSetMetadata(
            dataset_type="test_dict_ops",
            data={"description": "Dictionary operations test"},
        )

        dataset = DataSet(metadata=metadata)

        # Create a test section
        float_data = FloatData(value=99.9)
        await context.db.save(float_data)

        section = DataSetSection()
        section.add_model_group((float_data,))

        # Test setitem and getitem
        dataset["test"] = section

        engine = current_engine_context.get()
        await dataset.save(engine)
        assert dataset["test"] == section

        # Test keys, values, items
        assert list(dataset.keys()) == ["test"]
        assert list(dataset.values()) == [section]
        assert list(dataset.items()) == [("test", section)]

        # Test get with default
        assert dataset.get("test") == section
        assert dataset.get("nonexistent") is None

        # Test pop
        popped = dataset.pop("test")
        assert popped == section
        assert len(dataset) == 0

        # Test setdefault
        new_section = DataSetSection()
        returned = dataset.setdefault("new", new_section)
        assert returned == new_section
        assert dataset["new"] == new_section

    @pytest.mark.asyncio
    async def test_dataset_persistence(self):
        """Test saving and loading DataSet from the database."""
        # Create metadata
        metadata = DataSetMetadata(
            dataset_type="test_persistence",
            data={"version": "1.0", "created": datetime.now()},
        )

        # Create test models
        float_data = FloatData(value=123.45)
        string_data = StringData(value="persistence_test")

        await context.db.save(float_data)
        await context.db.save(string_data)

        # Create dataset with section
        section = DataSetSection()
        section.add_model_group((float_data, string_data))

        dataset = DataSet(metadata=metadata)
        dataset["main"] = section

        # Save dataset
        await context.db.save(dataset)
        dataset_id = dataset.id

        # Load dataset from database
        loaded_dataset = await context.db.find_one(DataSet, DataSet.id == dataset_id)

        assert loaded_dataset is not None
        assert loaded_dataset.dataset_type == "test_persistence"
        assert len(loaded_dataset) == 1
        assert "main" in loaded_dataset

        # Test retrieving models from the loaded section
        loaded_section = loaded_dataset["main"]
        retrieved_models = loaded_section.get_model_group(0)

        assert len(retrieved_models) == 2
        assert isinstance(retrieved_models[0], FloatData)
        assert isinstance(retrieved_models[1], StringData)
        assert retrieved_models[0].value == 123.45
        assert retrieved_models[1].value == "persistence_test"

    @pytest.mark.asyncio
    async def test_complex_dataset_workflow(self, real_database_context):
        """Test a complex workflow with multiple model types and sections."""
        # Create metadata
        metadata = DataSetMetadata(
            dataset_type="test_complex_workflow",
            data={"experiment": "ML_Pipeline", "version": "2.1", "samples": 1000},
        )

        # Create various test models - we'll create multiple node instances
        models = []
        for i in range(5):
            float_model = FloatData(value=float(i * 10))
            string_model = StringData(value=f"sample_{i}")

            # Create a new NodeRegistry for each iteration
            from simstack.models.parameters import Parameters

            parameters = Parameters()
            node_model = NodeRegistry(
                name=f"node_{i}",
                status="completed",
                input_ids=[],
                result_ids=[],
                function_hash=f"test_function_hash_{i}",
                arg_hash=f"test_arg_hash_{i}",
                func_mapping="test.module.function",
                parameters=parameters,
            )

            models.extend([float_model, string_model, node_model])
            for model in [float_model, string_model, node_model]:
                await context.db.save(model)

        # Create a dataset with multiple sections
        dataset = DataSet(metadata=metadata)

        # Training section: (FloatData, StringData) tuples
        training_section = DataSetSection()
        for i in range(0, 6, 3):  # indices 0, 3
            training_section.add_model_group((models[i], models[i + 1]))

        # Validation section: (FloatData, StringData) tuples
        validation_section = DataSetSection()
        for i in range(9, 15, 3):  # indices 9, 12
            validation_section.add_model_group((models[i], models[i + 1]))

        # Node section: single NodeRegistry tuples
        node_section = DataSetSection()
        for i in range(2, 15, 3):  # indices 2, 5, 8, 11, 14
            node_section.add_model_group((models[i],))

        dataset["training"] = training_section
        dataset["validation"] = validation_section
        dataset["nodes"] = node_section

        # Save and verify
        engine = current_engine_context.get()
        await dataset.save(engine)

        assert len(dataset) == 3
        assert len(dataset["training"]) == 2
        assert len(dataset["validation"]) == 2
        assert len(dataset["nodes"]) == 5

        # Verify model types
        assert dataset["training"].model_types == ["FloatData", "StringData"]
        assert dataset["validation"].model_types == ["FloatData", "StringData"]
        assert dataset["nodes"].model_types == ["NodeRegistry"]

        # Test retrieval from each section
        training_tuple = dataset["training"].get_model_group(0)
        assert isinstance(training_tuple[0], FloatData)
        assert isinstance(training_tuple[1], StringData)

        node_tuple = dataset["nodes"].get_model_group(0)
        assert isinstance(node_tuple, NodeRegistry)

    # ---------------------------------------------------------------------
    # Additional tests for DataSet.save() structure validation
    # ---------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_dataset_save_same_type_same_structure_different_names_succeeds(
        self, node_registry, real_database_context
    ):
        """
        Saving a second dataset with the same dataset_type and the same structure
        should succeed even if section names differ.
        """
        # Common models
        f = FloatData(value=1.23)
        s = StringData(value="abc")
        node = node_registry
        await context.db.save(f)
        await context.db.save(s)

        # First dataset defines the structure under dataset_type "ds_struct_v1"
        meta1 = DataSetMetadata(dataset_type="ds_struct_v1", data={"desc": "v1"})
        ds1 = DataSet(metadata=meta1)

        sec_a = DataSetSection()
        sec_a.add_model_group((f, s))  # ["FloatData", "StringData"]

        sec_b = DataSetSection()
        sec_b.add_model_group((node,))  # ["NodeRegistry"]

        ds1["a"] = sec_a
        ds1["b"] = sec_b
        engine = current_engine_context.get()
        await ds1.save(engine)

        # Second dataset with SAME dataset_type but DIFFERENT section names, same structure
        meta2 = DataSetMetadata(dataset_type="ds_struct_v1", data={"desc": "v1 second"})
        ds2 = DataSet(metadata=meta2)

        sec_train = DataSetSection()
        sec_train.add_model_group((f, s))  # same ["FloatData", "StringData"]

        sec_nodes = DataSetSection()
        sec_nodes.add_model_group((node,))  # same ["NodeRegistry"]

        ds2["training"] = sec_train
        ds2["nodes"] = sec_nodes

        # Should not raise

        await ds2.save(engine)

        assert len(ds1) == 2
        assert len(ds2) == 2

    @pytest.mark.asyncio
    async def test_dataset_save_same_type_different_structure_fails(
        self, node_registry, real_database_context
    ):
        """
        Saving a second dataset with the same dataset_type but a different structure
        should fail with ValueError from DataSet.save().
        """
        # Common models
        f1 = FloatData(value=10.0)
        s1 = StringData(value="x")
        node = node_registry
        await context.db.save(f1)
        await context.db.save(s1)

        # First dataset establishes structure: {"pair": ["FloatData", "StringData"], "nodes": ["NodeRegistry"]}
        meta1 = DataSetMetadata(dataset_type="ds_struct_v2", data={"desc": "baseline"})
        ds1 = DataSet(metadata=meta1)

        sec_pair = DataSetSection()
        sec_pair.add_model_group((f1, s1))

        sec_nodes = DataSetSection()
        sec_nodes.add_model_group((node,))

        ds1["pair"] = sec_pair
        ds1["nodes"] = sec_nodes

        engine = current_engine_context.get()
        await ds1.save(engine)

        # Second dataset with SAME dataset_type but DIFFERENT structure: change "pair" to only ["FloatData"]
        f2 = FloatData(value=99.9)
        await context.db.save(f2)

        meta2 = DataSetMetadata(
            dataset_type="ds_struct_v2", data={"desc": "should fail"}
        )
        ds2 = DataSet(metadata=meta2)

        sec_pair_changed = DataSetSection()
        sec_pair_changed.add_model_group(
            (f2,)
        )  # ["FloatData"] instead of ["FloatData", "StringData"]

        ds2[
            "pair"
        ] = sec_pair_changed  # keep the same key to emphasize structural mismatch
        ds2["nodes"] = sec_nodes  # keep one section same

        with pytest.raises(
            ValueError, match="Section pair has different content in existing structure"
        ):
            await ds2.save(engine)
