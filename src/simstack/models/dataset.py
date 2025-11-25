from typing import Dict, Iterator, Union, Tuple, KeysView, ValuesView, ItemsView, List

from odmantic import Model, ObjectId, EmbeddedModel, Field, Reference

from simstack.core.asnyc_helper import async_helper
from simstack.core.context import context
from simstack.core.engine import current_engine_context
from simstack.models import simstack_model
from simstack.models.dataset_metadata import DataSetMetadata
from simstack.util.importer import import_class_by_name
from simstack.util.make_table import make_column_defs_instance
from simstack.util.make_table import make_table_entries_helper


@simstack_model
class DataSetSection(EmbeddedModel):
    """
    Represents a section of a dataset containing tuples of models.

    A DataSetSection is a list of tuples where all tuples contain the same types of models.
    For example, if one tuple contains (ModelA, ModelB), then all tuples in this section
    must contain (ModelA, ModelB) instances.

    :ivar model_types: List of model class names that define the structure of each tuple.
    :type model_types: List[str]
    :ivar data: List of tuples, where each tuple contains model IDs corresponding to model_types.
    :type data: List[List[ObjectId]]
    """

    model_types: List[str] = Field(
        default_factory=list
    )  # Class names of models in each tuple
    data: List[List[ObjectId]] = Field(
        default_factory=list
    )  # List of tuples (as lists of ObjectIds)

    model_config = {"extra": "forbid"}

    def add_model_group(self, models: Union[Model, Tuple[Model, ...]]) -> None:
        """
        Add a tuple of models to this section.

        :param models: Tuple of model instances to add
        :raises ValueError: If the model types don't match the section's expected types
        """
        if isinstance(models, Model):
            models = (models,)
        model_names = [model.__class__.__name__ for model in models]
        model_ids = [model.id for model in models]

        # If this is the first tuple, set the model types
        if not self.model_types:
            self.model_types = model_names
        else:
            # Verify that the model types match
            if model_names != self.model_types:
                raise ValueError(
                    f"Model types {model_names} don't match section's expected types {self.model_types}"
                )

        self.data.append(model_ids)

    async def make_column_defs(self):
        """
        Generate ag-grid column definitions for all model types in this section.

        :return: List of column definitions for ag-grid
        """
        column_defs = []
        if len(self.data) == 0:
            return column_defs
        engine = current_engine_context.get()
        for model_group_id, model_type in zip(self.data[0], self.model_types):
            model_class = await import_class_by_name(model_type)
            model_instance = await engine.find_one(
                model_class, model_class.id == model_group_id
            )
            model_columns = make_column_defs_instance(model_instance)
            column_defs.extend(model_columns)
        return column_defs

    async def make_table_entries(self):
        all_data = []
        engine = current_engine_context.get()
        for model_group_ids in self.data:
            data = []
            for model_group_id, model_type in zip(model_group_ids, self.model_types):
                model_class = await import_class_by_name(model_type)
                model_instance = await engine.find_one(
                    model_class, model_class.id == model_group_id
                )
                model_data = make_table_entries_helper(model_instance)
                data.append(model_data)
            all_data.append(data)
        return all_data

    @async_helper
    async def get_model_group(self, index: int) -> Tuple[Model, ...]:
        """
        Retrieve a tuple of models at the specified index.

        :param index: Index of the tuple to retrieve
        :return: Tuple of model instances
        """
        if index >= len(self.data):
            raise IndexError(
                f"Index {index} out of range for section with {len(self.data)} model groups"
            )

        model_ids = self.data[index]
        models = []

        for model_type, model_id in zip(self.model_types, model_ids):
            model_class = await import_class_by_name(model_type)
            model_instance = await context.db.find_one(
                model_class, model_class.id == model_id
            )
            if model_instance is None:
                raise ValueError(
                    f"Model of type {model_type} with id {model_id} not found"
                )
            models.append(model_instance)

        if len(models) == 1:
            return models[0]
        return tuple(models)

    def get_all_model_groups(self) -> List[Tuple[Model, ...]]:
        """
        Retrieve all tuples in this section.

        :return: List of tuples of model instances
        """
        all_tuples = []
        for i in range(len(self.data)):
            tuple_models = self.get_model_group(i)
            all_tuples.append(tuple_models)
        return all_tuples

    # List-like behavior methods
    def __len__(self) -> int:
        """Return the number of model groups in this section."""
        return len(self.data)

    def __getitem__(
        self, index: Union[int, slice]
    ) -> Union[Tuple[Model, ...], List[Tuple[Model, ...]]]:
        """
        Get model group(s) at the specified index or slice.

        :param index: Index or slice to retrieve
        :return: Single tuple or list of tuples of model instances
        """
        if isinstance(index, slice):
            indices = list(range(*index.indices(len(self.data))))
            return [self.get_model_group(i) for i in indices]
        else:
            return self.get_model_group(index)

    def __setitem__(self, index: int, value: Tuple[Model, ...]) -> None:
        """
        Set model group at the specified index.

        :param index: Index to set
        :param value: Tuple of model instances to set
        """
        if index >= len(self.data):
            raise IndexError(
                f"Index {index} out of range for section with {len(self.data)} model groups"
            )

        model_names = [model.__class__.__name__ for model in value]
        model_ids = [model.id for model in value]

        # Verify that the model types match
        if self.model_types and model_names != self.model_types:
            raise ValueError(
                f"Model types {model_names} don't match section's expected types {self.model_types}"
            )

        self.data[index] = model_ids

    def __delitem__(self, index: int) -> None:
        """
        Delete model group at the specified index.

        :param index: Index to delete
        """
        if index >= len(self.data):
            raise IndexError(
                f"Index {index} out of range for section with {len(self.data)} model groups"
            )
        del self.data[index]

    def append(self, models: Tuple[Model, ...]) -> None:
        """
        Append a tuple of models to the section.

        :param models: Tuple of model instances to append
        """
        self.add_model_group(models)

    def insert(self, index: int, models: Tuple[Model, ...]) -> None:
        """
        Insert a tuple of models at the specified index.

        :param index: Index to insert at
        :param models: Tuple of model instances to insert
        """
        model_names = [model.__class__.__name__ for model in models]
        model_ids = [model.id for model in models]

        # If this is the first tuple, set the model types
        if not self.model_types:
            self.model_types = model_names
        else:
            # Verify that the model types match
            if model_names != self.model_types:
                raise ValueError(
                    f"Model types {model_names} don't match section's expected types {self.model_types}"
                )

        self.data.insert(index, model_ids)

    def extend(self, models_list: List[Tuple[Model, ...]]) -> None:
        """
        Extend the section with multiple tuples of models.

        :param models_list: List of tuples of model instances to extend with
        """
        for models in models_list:
            self.add_model_group(models)

    def pop(self, index: int = -1) -> Tuple[Model, ...]:
        """
        Remove and return a model group at the specified index (default last).

        :param index: Index to pop (default -1 for last)
        :return: Tuple of model instances that was removed
        """
        if len(self.data) == 0:
            raise IndexError("pop from empty DataSetSection")

        # Get the models first before removing
        models = self.get_model_group(index)
        del self.data[index]
        return models

    def remove(self, models: Tuple[Model, ...]) -> None:
        """
        Remove the first occurrence of the specified tuple of models.

        :param models: Tuple of model instances to remove
        :raises ValueError: If the tuple is not found
        """
        model_ids = [model.id for model in models]
        try:
            self.data.remove(model_ids)
        except ValueError:
            raise ValueError(f"Tuple {models} not found in DataSetSection")

    def clear(self) -> None:
        """Remove all model groups from the section."""
        self.data.clear()
        self.model_types.clear()

    async def index(
        self, models: Tuple[Model, ...], start: int = 0, stop: int = None
    ) -> int:
        """
        Return the index of the first occurrence of the specified tuple of models.

        :param models: Tuple of model instances to find
        :param start: Start index for search
        :param stop: Stop index for search
        :return: Index of the tuple
        :raises ValueError: If the tuple is not found
        """
        model_ids = [model.id for model in models]
        if stop is None:
            stop = len(self.data)

        for i in range(start, min(stop, len(self.data))):
            if self.data[i] == model_ids:
                return i

        raise ValueError(f"Tuple {models} not found in DataSetSection")

    def count(self, models: Tuple[Model, ...]) -> int:
        """
        Return the number of occurrences of the specified tuple of models.

        :param models: Tuple of model instances to count
        :return: Number of occurrences
        """
        model_ids = [model.id for model in models]
        return self.data.count(model_ids)

    def reverse(self) -> None:
        """Reverse the order of model groups in the section."""
        self.data.reverse()

    def __iter__(self):
        """
        Iterate over model groups in the section.

        :return: Async iterator over tuples of model instances
        """
        for i in range(len(self.data)):
            yield self.get_model_group(i)

    def __contains__(self, models: Tuple[Model, ...]) -> bool:
        """
        Check if the specified tuple of models exists in the section.

        :param models: Tuple of model instances to check for
        :return: True if found, False otherwise
        """
        model_ids = [model.id for model in models]
        return model_ids in self.data

    def __bool__(self) -> bool:
        """Return True if the section is not empty."""
        return len(self.data) > 0

    def __repr__(self) -> str:
        """Return string representation of the section."""
        return (
            f"DataSetSection(model_types={self.model_types}, length={len(self.data)})"
        )


@simstack_model
class DataSet(Model):
    name: str = Field(default="dataset")
    metadata: DataSetMetadata = Reference()
    sections: Dict[str, DataSetSection] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @property
    def dataset_type(self) -> str:
        return self.metadata.dataset_type

    async def save(self, engine):
        # engine = current_engine_context.get()
        structure = self.collect_structure()
        ok = await self.metadata.validate_dict(structure)
        if not ok:
            raise ValueError("Metadata validation failed")
        await engine.save_unchecked(self)

    async def custom_model_dump(self, **kwargs) -> Dict[str, str]:
        """
        :return: dict with id
        """
        return {"id": str(self.id)}

    def collect_structure(self) -> Dict[str, List[str]]:
        """
        Returns a dictionary where keys are section names and values are lists of model types.

        :return: Dictionary mapping section names to their model types
        :rtype: Dict[str, List[str]]
        """
        return {
            section_name: section.model_types if len(section) > 0 else None
            for section_name, section in self.sections.items()
        }

    # Dict-like behavior methods
    def __getitem__(self, key: str) -> DataSetSection:
        return self.sections[key]

    def __setitem__(self, key: str, value: DataSetSection) -> None:
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
        if default is None:
            return self.sections.pop(key)
        return self.sections.pop(key, default)

    def popitem(self) -> Tuple[str, DataSetSection]:
        return self.sections.popitem()

    def clear(self) -> None:
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
