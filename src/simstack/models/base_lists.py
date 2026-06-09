from typing import List, TypeVar, Iterator, TYPE_CHECKING
from odmantic import Field, Model, ObjectId
from simstack.models.simstack_model import simstack_model
from simstack.util.generic_list_mixin import GenericListMixin
from simstack.util.object_list_mixin import ObjectListMixin

if TYPE_CHECKING:
    from simstack.models import StringData

T = TypeVar("T")

@simstack_model
class StringDataList(Model, ObjectListMixin["StringData"]):
    field_name: str = "string_data_list"
    elements: List[ObjectId] = Field(default_factory=list, description="List of StringData ObjectIDs")

@simstack_model
class StringList(Model, GenericListMixin[str]):
    field_name: str = "string_list"
    elements: List[str] = Field(default_factory=list, description="List of strings")

    def __iter__(self) -> Iterator[T]:
        return iter(self.elements)

    def __len__(self) -> int:
        return len(self.elements)

