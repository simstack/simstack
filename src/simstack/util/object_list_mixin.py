import re
from typing import Generic, Optional, List, Iterator, Type, Union, Iterable, TypeVar, TYPE_CHECKING

from simstack.models.document import Model, ObjectId
from simstack.util.generic_list_mixin import GenericListMixin

T = TypeVar("T")


class ObjectListMixin(GenericListMixin[ObjectId], Generic[T]):
    """
       Mixin class for lists of Model ObjectIDs.
       Stores ObjectId instances in `elements`, but allows interaction with Model instances.
       """

    @staticmethod
    def _normalize_elements_for_init(data: dict) -> tuple[dict, Optional[List[Model]]]:
        elements = data.get("elements")
        if not elements:
            return data, None

        elements = list(elements)
        contains_models = any(isinstance(element, Model) for element in elements)

        if not contains_models:
            return data, None

        if not all(isinstance(element, Model) for element in elements):
            raise ValueError("elements must contain either only model instances or only ObjectIds")

        normalized_data = dict(data)
        normalized_data["elements"] = [element.id for element in elements]
        return normalized_data, elements

    def __iter__(self) -> Iterator[T]:
        return iter(self._get_cache())

    def _get_model_class(self) -> Type[T]:
        # T is the first type argument of ObjectListMixin
        if hasattr(self, "__orig_bases__"):
            for base in self.__class__.__orig_bases__:
                if hasattr(base, "__origin__") and base.__origin__ is ObjectListMixin:
                    return base.__args__[0]
        raise RuntimeError(f"Could not determine model class for {self.__class__.__name__}")

    def _set_cache(self, cache: List[T]):
        object.__getattribute__(self, "__dict__")["_cache"] = cache
        return cache

    def _get_cache(self) -> List[T]:
        self_dict = object.__getattribute__(self, "__dict__")
        cache = self_dict.get("_cache",None)
        if cache is None:
            cache = []
            cache = self._set_cache(cache)
        return cache


    def append(self, element: T):
        if not isinstance(element, Model):
            raise ValueError("can only append models to ObjectListMixin")
        cache = self._get_cache()
        if element not in cache:
            cache.append(element)
        self._set_cache(cache)

        obj_id = getattr(element, "id", None)
        if obj_id is None:
            raise ValueError("attempting to append a model without id to an ObjectListMixin class")
        if obj_id not in self.elements:
            self.elements.append(obj_id)

    def extend(self, elements: Union[List[T], "ObjectListMixin[T]"]):
        for element in elements:
           self.append(element)

    def insert(self, index: int, element: T):
        self.elements.insert(index, element.id)
        cache = self._get_cache()
        if index <= len(cache):
            cache.insert(index, element)

    def remove(self, element: T):
        self.elements.remove(element.id)
        cache = self._get_cache()
        if element in cache:
            cache.remove(element)

    def pop(self, index: int = -1) -> T:
        self.elements.pop(index)
        cache = self._get_cache()
        if index < 0:
            index = len(self.elements) + 1 + index # elements already popped
        if index < len(cache):
            return cache.pop(index)
        else:
            # If it's not in cache, we might have a problem if it was never loaded.
            # But pop is usually used on loaded lists.
            raise IndexError("Index out of range for cache. Load elements first.")

    def get(self, index: int) -> T:
        """Get an element by index, loading it from DB if necessary."""
        if index < 0 or index >= len(self.elements):
            raise IndexError("list index out of range")

        cache = self._get_cache()
        if index < len(cache):
            return cache[index]

        raise IndexError("Index out of range for cache. Load elements first.")

    def count(self, element: T) -> int:
        return self.elements.count(element.id)

    def index(self, element: T, start: int = 0, stop: int = None) -> int:
        return self.elements.index(element.id, start, stop if stop is not None else len(self.elements))

    def reverse(self):
        self.elements.reverse()
        self._get_cache().reverse()

    def sort(self, key=None, reverse: bool = False):
        # This is tricky because we need to sort both.
        # Simplest is to sort the cache and then update elements.
        # But we might not have a full cache.
        if len(self._get_cache()) != len(self.elements):
            # We can't easily sort if we don't have all elements in cache.
            # For now, let's just sort the elements if key only depends on ObjectId,
            # or raise if it needs the Model.
            # Actually, standard list.sort() uses the objects.
            raise RuntimeError("Sort requires all elements to be loaded in cache. Call _load_all_to_cache() first.")

        cache = self._get_cache()
        cache.sort(key=key, reverse=reverse)
        self.elements[:] = [obj.id for obj in cache]

    def __setitem__(self, index: Union[int, slice], value: Union[T, List[T]]):
        if isinstance(index, slice):
            if isinstance(value, Iterable):
                ids = [v.id for v in value]
                self.elements[index] = ids
                cache = self._get_cache()
                # Slice assignment on cache is only safe if it matches the current loaded state
                # For simplicity, let's just clear cache or try to update if it fits
                if index.start is not None and index.stop is not None and index.stop <= len(cache):
                     cache[index] = list(value)
                self._set_cache(cache)
            else:
                raise TypeError("Can only assign an iterable to a slice")
        else:
            self.elements[index] = value.id
            cache = self._get_cache()
            if index < len(cache):
                cache[index] = value
            self._set_cache(cache)

    def __delitem__(self, index: Union[int, slice]):
        del self.elements[index]
        cache = self._get_cache()
        if isinstance(index, slice):
            if index.start is not None and index.stop is not None and index.stop <= len(cache):
                del cache[index]
            else:
                raise IndexError("Slice out of range for cache")
        else:
            if index < len(cache):
                del cache[index]
        self._set_cache(cache)

    def copy(self) -> "ObjectListMixin[T]":
        import copy
        return copy.copy(self)

    def __getitem__(self, index: Union[int, slice]) -> Union[T, List[T], ObjectId, List[ObjectId]]:
        cache = self._get_cache()
        if isinstance(index, slice):
            if index.start is not None and index.stop is not None and index.stop <= len(cache):
                return cache[index]
            return self.elements[index]
        else:
            if 0 <= index < len(cache):
                return cache[index]
            return self.elements[index]

    def find(self, pattern: str) -> Optional[T]:
        regex = re.compile(pattern)
        cache = self._get_cache()
        for obj in cache:
            name = getattr(obj, "name", None)
            if name and regex.search(name):
                return obj
        return None

    def find_all(self, pattern: str) -> Iterator[T]:
        regex = re.compile(pattern)
        cache = self._get_cache()
        for obj in cache:
            name = getattr(obj, "name", None)
            if name and regex.search(name):
                yield obj


    def delete_element(self, element: Union[T, ObjectId]):
        cache = self._get_cache()
        if isinstance(element, Model):
            obj_id = element.id
            if element in cache:
                cache.remove(element)
        else:
            obj_id = element
            # If it's an ObjectId, we might need to find the object in cache to remove it
            cache = [o for o in cache if getattr(o, "id", None) != obj_id]
        self._set_cache(cache)
        if obj_id in self.elements:
            self.elements.remove(obj_id)

    def clear(self):
        self.elements.clear()
        self._set_cache([])

    def __contains__(self, element: Union[T, ObjectId]) -> bool:
        if isinstance(element, Model):
            return element.id in self.elements
        elif isinstance(element, ObjectId):
            return element in self.elements
        return element in self._get_cache()

    def __len__(self) -> int:
        return len(self.elements)
