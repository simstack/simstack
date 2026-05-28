from typing import Dict, Iterator, Union, Tuple, KeysView, ValuesView, ItemsView, List
from odmantic import Model, ObjectId, EmbeddedModel, Field, Reference
from simstack.core.asnyc_helper import async_helper
from simstack.models import simstack_model
from simstack.models.dataset_metadata import DataSetMetadata
from simstack.util.make_table import make_column_defs_instance
from simstack.util.make_table import make_table_entries_helper


@simstack_model
class DataSetSection(EmbeddedModel):
    """
    Represents a section of a dataset containing dictionaries of models.

    A DataSetSection is a list of dictionaries where for each key, the values are of the same model type.

    :ivar model_types: Dictionary mapping keys to model class names.
    :type model_types: Dict[str, str]
    :ivar data: List of dictionaries mapping keys to ObjectIds.
    :type data: List[Dict[str, ObjectId]]
    """

    model_types: Dict[str, str] = Field(default_factory=dict)
    data: List[Dict[str, ObjectId]] = Field(default_factory=list)

    column_defs: List[Dict] = Field(default_factory=list)
    table_entries: List[List[Dict]] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    async def add_item(self, item: Dict[str, Optional[Model]]) -> None:
        """
        Add a dictionary of models to this section.

        :param item: Dictionary of model instances to add.
        :raises ValueError: If the model types don't match the section's expected types.
        """
        from simstack.core.context import context
        if isinstance(models, Model):
            models = (models,)
        model_names = [model.__class__.__name__ for model in models]

        # Verify that all the models are already stored, otherwise store them
        db = context.db
        stored_models = []
        model_ids = []
        for model in models:
            if model is None:
                model_ids.append(None)
                continue
            if model.id is None:
                stored_model = await db.save(model)
                stored_models.append(stored_model)
            else:
                self.model_types[key] = model_name

        # Save models if they don't have an ID
        item_ids = {}
        for key, model in filtered_item.items():
            if model.id is None:
                await engine.save_unchecked(model)
                item_ids[key] = model.id
            else:
                item_ids[key] = model.id

        self.data.append(item_ids)

    async def make_column_defs(self):
        """
        Generate ag-grid column definitions for all model types in this section.
        """

        from simstack.util.importer import import_class_by_name
        from simstack.core.context import context
        column_defs = []
        if not self.data:
            return column_defs
        db = context.db
        for model_group_id, model_type in zip(self.data[0], self.model_types):
            model_class = await import_class_by_name(model_type, db)
            model_instance = await db.find_one(
                model_class, model_class.id == model_group_id
            )
            if model_instance is None:
                raise ValueError(f"DB-Save Model of type {model_type} with id {model_group_id} not found")
            model_columns = make_column_defs_instance(model_instance)
            column_defs.extend(model_columns)
        return column_defs

    async def make_table_entries(self):
        from simstack.core.context import context
        from simstack.util.importer import import_class_by_name
        all_data = []
        db = context.db

        for model_group_ids in self.data:
            data = []
            for model_group_id, model_type in zip(model_group_ids, self.model_types):
                model_class = await import_class_by_name(model_type, db)
                model_instance = await db.find_one(
                   model_class, model_class.id == model_group_id
                )

                model_data = make_table_entries_helper(model_instance)
                row_data.append(model_data)
            all_data.append(row_data)
        return all_data

    async def get_item(self, index: int) -> Dict[str, Model]:
        if index < 0 or index >= len(self.data):
            raise IndexError("Index out of range")

        row = self.data[index]
        db = context.db
        result = {}
        for key, model_id in row.items():
            model_type = self.model_types[key]
            from simstack.util.importer import import_class_by_name
            model_class = await import_class_by_name(model_type, db)
            model_instance = await db.find_one(model_class, model_class.id == model_id)
            if model_instance is None:
                 raise ValueError(f"Model with id {model_id} of type {model_type} not found")
            result[key] = model_instance
        return result

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, index: Union[int, slice]):
        if isinstance(index, slice):
            return self.data[index]
        return self.data[index]

    def __repr__(self) -> str:
        return f"DataSetSection(keys={list(self.model_types.keys())}, length={len(self.data)})"


@simstack_model
class DataSet(Model):
    field_name: str = Field(default="dataset")
    metadata: DataSetMetadata = Reference()
    sections: Dict[str, DataSetSection] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @property
    def dataset_type(self) -> str:
        return self.metadata.dataset_type

    async def save(self, engine):
        structure = self.collect_structure()
        # metadata.validate_dict currently expects Dict[str, List[str]]
        # We might need to adjust it or pass something compatible.
        # For now, let's just pass the keys of model_types as a list.
        # Wait, DataSetTuple passes List[str] which are the model names in the tuple.
        # For DataSet, maybe it should be the model names too, but we lose the keys.
        # Let's see how validate_dict uses it.
        ok = await self.metadata.validate_dict(structure)
        if not ok:
            raise ValueError("Metadata validation failed")

        for key, section in self.sections.items():
            self.sections[key].column_defs = await section.make_column_defs()
            self.sections[key].table_entries = await section.make_table_entries()

        await engine.save_unchecked(self)

    def collect_structure(self) -> Dict[str, List[str]]:
        # This is a bit tricky. DataSetMetadata.validate_dict expects List[str].
        # If we want to stay compatible without changing DataSetMetadata yet:
        return {
            section_name: list(section.model_types.values()) if len(section) > 0 else None
            for section_name, section in self.sections.items()
        }

    def __getitem__(self, key: str) -> DataSetSection:
        if key not in self.sections:
            self.sections[key] = DataSetSection()
        return self.sections[key]

    def __setitem__(self, key: str, value: DataSetSection) -> None:
        if key in self.sections:
            raise KeyError(f"Section {key} already exists in dataset")
        self.sections[key] = value

    def __delitem__(self, key: str) -> None:
        del self.sections[key]

    def __len__(self) -> int:
        return len(self.sections)

    def __iter__(self) -> Iterator[str]:
        return iter(self.sections)

    def __contains__(self, key: str) -> bool:
        return key in self.sections

    def keys(self) -> KeysView[str]:
        return self.sections.keys()

    def values(self) -> ValuesView[DataSetSection]:
        return self.sections.values()

    def items(self) -> ItemsView[str, DataSetSection]:
        return self.sections.items()

    def get(self, key: str, default: DataSetSection = None) -> DataSetSection:
        return self.sections.get(key, default)

    def pop(self, key: str, default=None) -> DataSetSection:
        return self.sections.pop(key, default)

    def clear(self):
        self.sections.clear()

    def update(
        self, other: Union[Dict[str, DataSetSection], "DataSet"] = None, **kwargs
    ) -> None:
        if other is not None:
            if hasattr(other, "sections"):
                self.sections.update(other.sections)
            else:
                self.sections.update(other)
        self.sections.update(kwargs)

    def setdefault(self, key: str, default: DataSetSection = None) -> DataSetSection:
        return self.sections.setdefault(key, default)

    @classmethod
    def ui_schema(cls) -> dict:
        return {
            "ui:field": "DataSetField",
            "metadata": {"ui:widget": "hidden"},
            "sections": {"ui:widget": "hidden"},
        }


class DataSetSelectionField(EmbeddedModel):
    section_name: str = Field(default="default")
    indices: List[int] = Field(default_factory=list)

@simstack_model
class DataSetSelection(Model):
    field_name: str = Field(default="dataset_selection")
    dataset_id: ObjectId
    dataset_selection_fields: List[DataSetSelectionField] = Field(default_factory=list)

    async def get_dataset(self):
        from simstack.core.context import context
        return await context.db.find_one(DataSet, DataSet.id == self.dataset_id)

    @async_helper
    async def get_selected_elements(self, section_name: str = None) -> List[Tuple[Model, ...]]:
        """
        Retrieve all selected model groups from the dataset.

        :param section_name: Optional section name to filter results. If None, returns all sections.
        :return: List of tuples of model instances for all selected elements
        """
        from simstack.core.context import context
        db = context.db
        dataset = await db.find_one(DataSet, DataSet.id == self.dataset_id)

        if dataset is None:
            raise ValueError(f"Dataset with id {self.dataset_id} not found")

        selected_elements = []
        for selection_field in self.dataset_selection_fields:
            if section_name is not None and selection_field.section_name != section_name:
                continue

            section = dataset.sections.get(selection_field.section_name)
            if section is None:
                raise ValueError(f"Section {selection_field.section_name} not found in dataset")

            for index in selection_field.indices:
                if index >= len(section):
                    raise IndexError(
                        f"Index {index} out of range for section {selection_field.section_name} with {len(section)} elements"
                    )
                model_group = section.get_model_group(index)
                selected_elements.append(model_group)

        return selected_elements

    async def __aiter__(self, section_name: str = None):
        """
        Async iterator over all selected model groups.

        :param section_name: Optional section name to filter results. If None, returns all sections.
        :return: Async iterator yielding tuples of model instances
        """
        from simstack.core.context import context
        db = context.db
        dataset = await db.find_one(DataSet, DataSet.id == self.dataset_id)

        if dataset is None:
            raise ValueError(f"Dataset with id {self.dataset_id} not found")

        for selection_field in self.dataset_selection_fields:
            if section_name is not None and selection_field.section_name != section_name:
                continue

            section = dataset.sections.get(selection_field.section_name)
            if section is None:
                raise ValueError(f"Section {selection_field.section_name} not found in dataset")

            for index in selection_field.indices:
                if index >= len(section):
                    raise IndexError(
                        f"Index {index} out of range for section {selection_field.section_name} with {len(section)} elements"
                    )
                yield section.get_model_group(index)

    @classmethod
    def ui_schema(cls) -> dict:
        return {
            "ui:field": "DataSetSelectionField",
        }

