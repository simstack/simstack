from typing import List

from odmantic import Model, Field

from simstack.models import simstack_model
from simstack.models.array_storage import ArrayStorage


@simstack_model
class ArrayList(Model):
    array_list: List[ArrayStorage] = Field(default_factory=list)

    def append(self, array_storage: ArrayStorage):
        """Add a new ArrayStorage to the list"""
        self.array_list.append(array_storage)

    def get(self, index: int) -> ArrayStorage:
        """Get ArrayStorage at specified index"""
        return self.array_list[index]

    def remove(self, index: int):
        """Remove ArrayStorage at specified index"""
        del self.array_list[index]

    @property
    def length(self) -> int:
        """Get number of items in the list"""
        return len(self.array_list)

    def clear(self):
        """Remove all items from the list"""
        self.array_list.clear()

    def __iter__(self):
        """Enable iteration over array_list"""
        return iter(self.array_list)

    def __getitem__(self, index: int) -> ArrayStorage:
        """Enable access using square brackets"""
        return self.array_list[index]

    def __setitem__(self, index: int, value: ArrayStorage):
        """Enable item assignment using square brackets"""
        self.array_list[index] = value

    def __len__(self) -> int:
        """Enable len() function"""
        return len(self.array_list)
