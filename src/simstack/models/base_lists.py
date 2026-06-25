import re
from typing import Union, List, Any, Optional, TypeVar, Generic, Iterator, Iterable

from odmantic import EmbeddedModel, Field, Model

from simstack.models import simstack_model, StringData
from simstack.models.files import FileStack


T = TypeVar("T")


class GenericListMixin(Generic[T]):
    """
    Mixin class containing common functionality for list operations.

    Notes on typing:
      - Pure list-like operations (append/extend/...) are fully generic over `T`.
      - Convenience helpers like `find()` / `filter_by_size()` rely on optional attributes
        (e.g. `.name`, `.size`). For arbitrary `T`, we use `getattr()` to keep runtime
        behavior flexible while remaining type-safe-ish.
    """

    def __len__(self):
        return len(self.elements)

    def append(self, elements: T):
        self.elements.append(elements)

    def extend(self, elements: Union[List[T], "GenericListMixin[T]"]):
        if hasattr(elements, "elements"):
            # It's another GenericListMixin object
            self.elements.extend(elements.elements)
        else:
            # It's a list
            self.elements.extend(elements)

    def insert(self, index: int, elements: T):
        self.elements.insert(index, elements)

    def remove(self, elements: T):
        self.elements.remove(elements)

    def pop(self, index: int = -1) -> T:
        return self.elements.pop(index)

    def clear(self):
        self.elements.clear()

    def index(self, elements: T, start: int = 0, stop: int = None) -> int:
        if stop is None:
            return self.elements.index(elements, start)
        return self.elements.index(elements, start, stop)

    def count(self, elements: T) -> int:
        return self.elements.count(elements)

    def reverse(self):
        self.elements.reverse()

    def sort(self, key=None, reverse: bool = False):
        self.elements.sort(key=key, reverse=reverse)

    def copy(self) -> List[T]:
        return self.elements.copy()

    def __getitem__(self, index: Union[int, slice]) -> Union[T, List[T]]:
        return self.elements[index]

    def __setitem__(self, index: Union[int, slice], value: Union[T, List[T]]):
        self.elements[index] = value

    def __delitem__(self, index: Union[int, slice]):
        del self.elements[index]

    def __iter__(self) -> Iterator[T]:
        return iter(self.elements)

    def __contains__(self, elements: T) -> bool:
        return elements in self.elements

    def __bool__(self) -> bool:
        return bool(self.elements)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(elements={self.elements!r})"

    def find(self, pattern: str) -> Optional[T]:
        for elements in self.elements:
            name = getattr(elements, "name", None)
            if name and pattern == name:  # re.search(pattern, name):
                return elements
        return None

    def find_all(self, pattern: str) -> List[T]:
        matches: List[T] = []
        for elements in self.elements:
            name = getattr(elements, "name", None)
            if name and re.search(pattern, name):
                matches.append(elements)
        return matches

    def filter_by_size(self, min_size: int = None, max_size: int = None) -> List[T]:
        results: List[T] = []
        for elements in self.elements:
            size = getattr(elements, "size", None)
            if size is None:
                continue
            if min_size is not None and size < min_size:
                continue
            if max_size is not None and size > max_size:
                continue
            results.append(elements)
        return results

    def filter_by_property(self, property_name: str, value: Any) -> List[T]:
        results: List[T] = []
        for elements in self.elements:
            if hasattr(elements, property_name) and getattr(elements, property_name) == value:
                results.append(elements)
        return results

    def sort_by_name(self, reverse: bool = False):
        self.elements.sort(key=lambda x: getattr(x, "name", "") or "", reverse=reverse)

    def sort_by_size(self, reverse: bool = False):
        self.elements.sort(key=lambda x: getattr(x, "size", 0) or 0, reverse=reverse)

    # def items(self) -> Iterator[tuple[Optional[str], T]]:
    #     """
    #     Iterator that yields (name, element) tuples for each element in the list.
    #     Similar to dict.items() behavior.
    #
    #     For arbitrary `T`, `name` is fetched via getattr and may be None.
    #     """
    #     for elements in self.elements:
    #         yield (getattr(elements, "name", None), elements)


@simstack_model
class StringList(Model, GenericListMixin[str]):
    field_name: str = "string_list"
    elements: List[str] = Field(default_factory=list, description="List of strings")

    def __iter__(self) -> Iterator[T]:
        return iter(self.elements)

    def __len__(self) -> int:
        return len(self.elements)
