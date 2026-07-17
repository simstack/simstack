from typing import Iterator, List, TypeVar

from odmantic import Field, Model, ObjectId

from simstack.models.base_types import BooleanData, StringData
from simstack.models.simstack_model import simstack_model
from simstack.util.generic_list_mixin import GenericListMixin
from simstack.util.object_list_mixin import ObjectListMixin

T = TypeVar("T")

@simstack_model
class StringDataList(Model, ObjectListMixin[StringData]):
    field_name: str = "string_data_list"
    elements: List[ObjectId] = Field(default_factory=list, description="List of StringData ObjectIDs")

    def __iter__(self) -> Iterator[StringData]:
        return ObjectListMixin.__iter__(self)

    def __init__(self, **data):
        data, cache = self._normalize_elements_for_init(data)
        Model.__init__(self, **data)
        if cache is not None:
            self._set_cache(cache)


@simstack_model
class BooleanDataList(Model, ObjectListMixin[BooleanData]):
    field_name: str = "boolean_data_list"
    elements: List[ObjectId] = Field(default_factory=list, description="List of BooleanData ObjectIDs")

    def __iter__(self) -> Iterator[BooleanData]:
        return ObjectListMixin.__iter__(self)

    def __init__(self, **data):
        data, cache = self._normalize_elements_for_init(data)
        Model.__init__(self, **data)
        if cache is not None:
            self._set_cache(cache)


@simstack_model
class StringList(Model, GenericListMixin[str]):
    field_name: str = "string_list"
    elements: List[str] = Field(default_factory=list, description="List of strings")

    def __iter__(self) -> Iterator[T]:
        return iter(self.elements)

    def __len__(self) -> int:
        return len(self.elements)
