from typing import List

from odmantic import Model, Field, ObjectId

from simstack.models import simstack_model
from simstack.models.array_storage import ArrayStorage
from simstack.util.object_list_mixin import ObjectListMixin


@simstack_model
class ArrayList(Model, ObjectListMixin[ArrayStorage]):
    elements: List[ObjectId] = Field(default_factory=list)

    def __init__(self, **data):
        data, cache = self._normalize_elements_for_init(data)
        Model.__init__(self, **data)
        if cache is not None:
            self._set_cache(cache)

    def __iter__(self):
        return ObjectListMixin.__iter__(self)
