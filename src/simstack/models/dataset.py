from typing import Dict, Iterator, Union, Tuple, KeysView, ValuesView, ItemsView, List, Optional
from odmantic import Model, ObjectId, EmbeddedModel, Field, Reference

from simstack.core.asnyc_helper import async_helper
from simstack.core.engine import current_engine_context
from simstack.models import simstack_model
from simstack.models.dataset_metadata import DataSetMetadata
from simstack.util.importer import import_class_by_name
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
        engine = current_engine_context.get()
        
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
        column_defs = []
        if not self.data:
            return column_defs
        
        engine = current_engine_context.get()
        # Use model_types to determine columns. 
        # Since it's a dict, we might want to order them or just iterate.
        for key, model_type in self.model_types.items():
            # Find the first instance of this key in data to get an ID for make_column_defs_instance if needed
            # Actually make_column_defs_instance might just need the class, let's check how it's used in DataSetTuple
            
            # In DataSetTuple:
            # for model_group_id, model_type in zip(self.data[0], self.model_types):
            #     model_class = await import_class_by_name(model_type)
            #     model_instance = await engine.find_one(model_class, model_class.id == model_group_id)
            #     model_columns = make_column_defs_instance(model_instance)
            #     column_defs.extend(model_columns)
            
            # We do something similar but we need an instance for each key.
            model_group_id = None
            for row in self.data:
                if key in row:
                    model_group_id = row[key]
                    break
            
            if model_group_id is None:
                continue
                
            model_class = await import_class_by_name(model_type)
            model_instance = await engine.find_one(model_class, model_class.id == model_group_id)
            if model_instance:
                model_columns = make_column_defs_instance(model_instance)
                # Maybe prefix column headers with the key? 
                # DataSetTuple doesn't seem to prefix, but it's a tuple so order matters.
                # In a dict, we might have many models.
                column_defs.extend(model_columns)
        
        return column_defs

    async def make_table_entries(self):
        all_data = []
        engine = current_engine_context.get()

        for row in self.data:
            row_data = []
            for key, model_type in self.model_types.items():
                model_group_id = row.get(key)
                if model_group_id is None:
                    # How to handle missing values in make_table_entries?
                    # DataSetTuple assumes all models in the tuple are present (or at least zip handles it)
                    # Let's see what make_table_entries_helper does with None.
                    row_data.append({}) # Or some empty representation
                    continue
                
                model_class = await import_class_by_name(model_type)
                model_instance = await engine.find_one(model_class, model_class.id == model_group_id)
                model_data = make_table_entries_helper(model_instance)
                row_data.append(model_data)
            all_data.append(row_data)
        return all_data

    async def get_item(self, index: int) -> Dict[str, Model]:
        if index < 0 or index >= len(self.data):
            raise IndexError("Index out of range")
        
        row = self.data[index]
        engine = current_engine_context.get()
        result = {}
        for key, model_id in row.items():
            model_type = self.model_types[key]
            model_class = await import_class_by_name(model_type)
            model_instance = await engine.find_one(model_class, model_class.id == model_id)
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
