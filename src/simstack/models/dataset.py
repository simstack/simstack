import uuid
from typing import Dict, Iterator, Union, Tuple, KeysView, ValuesView, ItemsView, List, Optional, Any
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
    :ivar data: Dictionary mapping names to dictionaries mapping keys to ObjectIds.
    :type data: Dict[str, Dict[str, ObjectId]]
    """

    model_types: Dict[str, str] = Field(default_factory=dict)

    data: Dict[str, Dict[str, ObjectId]] = Field(default_factory=dict)

    column_defs: List[Dict] = Field(default_factory=list)
    table_entries: List[List[Dict]] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    def _set_cache(self, cache: Dict[str, Dict[str, Model]]):
        object.__getattribute__(self, "__dict__")["_cache"] = cache
        return cache

    def _get_cache(self) -> Dict[str, Dict[str, Model]]:
        self_dict = object.__getattribute__(self, "__dict__")
        cache = self_dict.get("_cache", None)
        if cache is None:
            cache = {}
            cache = self._set_cache(cache)
        return cache

    def add_row(self, item: Dict[str, Optional[Model]], name: Optional[str] = None) -> None:
        """
        Add a dictionary of models to this section.

        :param item: Dictionary of model instances to add.
        :param name: Optional name for the item. If None, a UUID will be generated.
        :raises ValueError: If the model types don't match the section's expected types.
        :raises TypeError: If a non-None item value is not a Model instance.
        """

        if name is None:
            name = str(uuid.uuid4())

        cache = self._get_cache()
        if name in cache:
            raise ValueError(f"Item with name '{name}' already exists in section")
        
        if not isinstance(item, dict):
            raise TypeError("Item must be a dictionary")
        
        # Type check: raise TypeError if an item which is not None is not a Model
        for key, value in item.items():
            if value is not None and not isinstance(value, Model):
                raise TypeError(
                    f"Item with key '{key}' is not a Model instance: {type(value).__name__}"
                )

        filtered_item = {k: v for k, v in item.items() if v is not None}

        current_item_types = {k: v.__class__.__name__ for k, v in filtered_item.items()}

        # If this is the first item, we can partially or fully initialize model_types
        # However, subsequent items might have new keys.
        # The requirement says: "all items with the same key must be the same Model type"

        for key, model_name in current_item_types.items():
            if key in self.model_types:
                if self.model_types[key] != model_name:
                    raise ValueError(
                        f"Model type for key '{key}' is {model_name}, but expected {self.model_types[key]}"
                    )
            else:
                self.model_types[key] = model_name

        # Save all models to cache
        cached_items = {}
        for key, model in item.items():
            if model is None:
                continue
            cached_items[key] = model

        cache[name] = cached_items

    async def make_column_defs(self):
        """
        Generate ag-grid column definitions for all model types in this section.
        """

        from simstack.util.importer import import_class_by_name
        from simstack.core.context import context
        engine = context.db
        column_defs = []
        cache = self._get_cache()
        if not cache:
            return column_defs

        # Use model_types to determine columns. 
        for key, model_type in self.model_types.items():
            model_instance = None
            for row in cache.values():
                if key in row:
                    model_instance = row[key]
                    break
            
            if model_instance is None:
                continue
                
            model_columns = make_column_defs_instance(model_instance)
            column_defs.extend(model_columns)
        
        return column_defs

    async def make_table_entries(self):
        all_data = []
        cache = self._get_cache()
        for row in cache.values():
            row_data = []
            for key, model_type in self.model_types.items():
                model_instance = row.get(key)
                model_data = make_table_entries_helper(model_instance)
                row_data.append(model_data)
            all_data.append(row_data)
        return all_data


    async def save(self, db):
        """
        Save all models in the cache to the database and update data.
        """
        cache = self._get_cache()
        for row_name, row_models in cache.items():
            for model_key, model in row_models.items():
                if model is not None:
                    await db.save_unchecked(model)

        # Sync cache to data before saving
        self.data = {
            name: {k: v.id for k, v in row.items()}
            for name, row in cache.items()
        }

        self.column_defs = await self.make_column_defs()
        self.table_entries = await self.make_table_entries()

    def get_item(self, name: str) -> Dict[str, Model]:
        cache = self._get_cache()
        if name not in cache:
            raise KeyError(f"Item with name '{name}' not found")
        return cache[name]

    async def db_find_postprocess(self, db: "Database"):
        await self.load_to_cache(db)

    async def load_to_cache(self, db: "Database") -> None:
        """
        Load all items from the database into the cache assuming that data is already loaded.
        """
        from simstack.core.context import context
        if db is None:
            db = context.db
        cache = self._get_cache()
        for name, row in self.data.items():
            cached_row = {}
            for key, model_id in row.items():
                model_type = self.model_types[key]
                from simstack.util.importer import import_class_by_name
                model_class = await import_class_by_name(model_type, db)
                model_instance = await db.find_one(model_class, model_class.id == model_id)
                if model_instance is None:
                    raise ValueError(f"Model with id {model_id} of type {model_type} not found")
                cached_row[key] = model_instance

            # Update cache
            cache[name] = cached_row
        self._set_cache(cache)

    def __len__(self) -> int:
        return len(self._get_cache())

    def __iter__(self):
        return iter(self._get_cache().items())

    def __getitem__(self, name: str) -> Dict[str, Model]:
        cache = self._get_cache()
        if name in cache:
            return cache[name]
        
        raise KeyError(f"Item with name '{name}' not found")

    def __setitem__(self, name: str, value: Dict[str, Optional[Model]]) -> None:
        cache = self._get_cache()
        if name in cache:
             self.pop(name, None)
        
        self.add_row(value, name=name)

    def __delitem__(self, name: str) -> None:
        cache = self._get_cache()
        if name not in cache:
            raise KeyError(f"Item with name '{name}' not found")
        del cache[name]

    def __contains__(self, name: str) -> bool:
        return name in self._get_cache()

    def get(self, name: str, default: Any = None) -> Any:
        try:
            return self[name]
        except KeyError:
            return default

    def keys(self) -> KeysView[str]:
        return self._get_cache().keys()

    def values(self) -> ValuesView[Dict[str, Model]]:
        return self._get_cache().values()

    def items(self) -> ItemsView[str, Dict[str, Model]]:
        return self._get_cache().items()

    def clear(self) -> None:
        self._set_cache({})

    def pop(self, name: str, default: Any = ...) -> Any:
        cache = self._get_cache()
        if name not in cache:
            if default is ...:
                raise KeyError(f"Item with name '{name}' not found")
            return default
        
        return cache.pop(name)

    def popitem(self) -> Tuple[str, Dict[str, Model]]:
        cache = self._get_cache()
        if not cache:
            raise KeyError("popitem(): dictionary is empty")
        return cache.popitem()

    def update(self, other: Union[Dict[str, Dict[str, Optional[Model]]], "DataSetSection"]) -> None:
        if isinstance(other, DataSetSection):
            other_cache = other._get_cache()
            for name, row in other_cache.items():
                self[name] = row
        else:
            for name, row in other.items():
                self[name] = row

    def setdefault(self, name: str, default: Dict[str, Optional[Model]] = None) -> Dict[str, Model]:
        cache = self._get_cache()
        if name not in cache:
            self[name] = default
        return self[name]

    def __repr__(self) -> str:
        return f"DataSetSection(keys={list(self.model_types.keys())}, length={len(self)})"


@simstack_model
class DataSet(Model):
    field_name: str = Field(default="dataset")
    metadata: DataSetMetadata
    sections: Dict[str, DataSetSection] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @property
    def dataset_type(self) -> str:
        return self.metadata.dataset_type

    async def save(self, db):
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
            await section.save(db)

        await db.save_unchecked(self)

    def collect_structure(self) -> Dict[str, Dict[str, str]]:
        return {
            section_name: section.model_types if len(section) > 0 else {}
            for section_name, section in self.sections.items() if len(section) > 0
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

