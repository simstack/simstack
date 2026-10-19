import uuid
from pydantic import model_validator
from typing import ClassVar, Dict, Iterator, Union, Tuple, KeysView, ValuesView, ItemsView, List, Optional, Any, Literal
from simstack.models.document import EmbeddedModel, Field, Model, ObjectId
from simstack.models import simstack_model
from simstack.models.dataset_metadata import DataSetMetadata
from simstack.util.make_table import make_column_defs_instance
from simstack.util.make_table import make_table_entries_helper
from simstack.models.dataset_storage import (DatasetRootMixin, DatasetSectionMixin, SharedDatasetSectionStorage,
    SharedDatasetSelectionStorage, SharedDatasetSelectionChange, section_cache, section_data, set_section_data,
    section_row_ids)


@simstack_model
class DataSetSection(DatasetSectionMixin, EmbeddedModel):
    """
    Represents a section of a dataset containing dictionaries of models.

    A DataSetSection is a list of dictionaries where for each key, the values are of the same model type.

    :ivar model_types: Dictionary mapping keys to model class names.
    :type model_types: Dict[str, str]
    :ivar data: Dictionary mapping names to dictionaries mapping keys to ObjectIds.
    :type data: Dict[str, Dict[str, ObjectId]]
    """

    model_types: Dict[str, str] = Field(default_factory=dict)

    __dataset_kind__: ClassVar[str] = "dict"
    inline_data: Dict[str, Dict[str, ObjectId]] = Field(default_factory=dict, alias="data", serialization_alias="data")
    storage: SharedDatasetSectionStorage | None = None

    column_defs: List[Dict] = Field(default_factory=list)
    table_entries: List[List[Dict]] = Field(default_factory=list)

    model_config = {"extra": "forbid", "serialize_by_alias": True}

    @property
    def data(self):
        return section_data(self)

    @data.setter
    def data(self, value):
        set_section_data(self, value)

    def _set_cache(self, cache: Dict[str, Dict[str, Model]]):
        object.__getattribute__(self, "__dict__")["_cache"] = cache
        return cache

    def _get_cache(self) -> Dict[str, Dict[str, Model]]:
        section_cache(self)
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
        ids = section_row_ids(self)
        removed = object.__getattribute__(self, "__dict__").get("_dataset_removed_ids", {})
        ids[name] = removed.pop(id(item), ids[name])

    async def make_column_defs(self):
        """
        Generate ag-grid column definitions for all model types in this section.
        """

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


    def get_item(self, name: str) -> Dict[str, Model]:
        cache = self._get_cache()
        if name not in cache:
            raise KeyError(f"Item with name '{name}' not found")
        return cache[name]

    def __len__(self) -> int:
        if self.storage is not None and object.__getattribute__(self, "__dict__").get("_cache") is None:
            return self.storage.row_count
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
        identity = section_row_ids(self).get(name)
        if name in cache:
             self.pop(name, None)
        
        self.add_row(value, name=name)
        if identity is not None:
            section_row_ids(self)[name] = identity

    def __delitem__(self, name: str) -> None:
        cache = self._get_cache()
        if name not in cache:
            raise KeyError(f"Item with name '{name}' not found")
        self.pop(name)

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
        object.__getattribute__(self, "__dict__")["_dataset_row_ids"] = {}

    def pop(self, name: str, default: Any = ...) -> Any:
        cache = self._get_cache()
        if name not in cache:
            if default is ...:
                raise KeyError(f"Item with name '{name}' not found")
            return default
        
        row = cache.pop(name)
        ids = section_row_ids(self)
        identity = ids.pop(name, None)
        if identity:
            object.__getattribute__(self, "__dict__").setdefault("_dataset_removed_ids", {})[id(row)] = identity
        return row

    def popitem(self) -> Tuple[str, Dict[str, Model]]:
        cache = self._get_cache()
        if not cache:
            raise KeyError("popitem(): dictionary is empty")
        name = next(reversed(cache))
        return name, self.pop(name)

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
class DataSet(DatasetRootMixin, Model):
    __dataset_kind__: ClassVar[str] = "dict"
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    field_name: str = Field(default="dataset")
    metadata: DataSetMetadata
    sections: Dict[str, DataSetSection] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @property
    def dataset_type(self) -> str:
        return self.metadata.dataset_type

    def collect_structure(self) -> Dict[str, Dict[str, str]]:
        return {
            section_name: section.model_types
            for section_name, section in self.sections.items()
            if (section.storage.row_count if section.storage is not None else bool(section.inline_data) or len(section) > 0)
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
            "ui:options": {"datasetKind": "dict"},
            "metadata": {"ui:widget": "hidden"},
            "sections": {"ui:widget": "hidden"},
        }


class DataSetSelectionField(EmbeddedModel):
    section_name: str = Field(default="default")
    indices: List[int] = Field(default_factory=list)
    section_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{24}$")
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    row_ids: List[str] = Field(default_factory=list)
    selected_count: int = Field(default=0, ge=0)

@simstack_model
class DataSetSelection(Model):
    __dataset_selection_kind__: ClassVar[str] = "dict"
    kind: Literal["dict"] = "dict"
    source_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    storage: SharedDatasetSelectionStorage | None = None
    selection_changes: List[SharedDatasetSelectionChange] = Field(default_factory=list)
    field_name: str = Field(default="dataset_selection")
    dataset_id: ObjectId
    dataset_selection_fields: List[DataSetSelectionField] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_normalized_indices(self):
        if (self.source_revision is not None or self.storage is not None) and any(field.indices for field in self.dataset_selection_fields):
            raise ValueError("Normalized dataset selections require stable row IDs, not positional indices")
        return self

    @classmethod
    def ui_schema(cls) -> dict:
        return {
            "ui:field": "DataSetSelectionField",
            "ui:options": {"datasetKind": "dict"},
        }
