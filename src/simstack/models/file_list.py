import re
from typing import List, Optional, Iterator

from odmantic import EmbeddedModel, Field, Model, ObjectId

from simstack.models import simstack_model
from simstack.util.object_list_mixin import ObjectListMixin
from simstack.models.files import FileStack


@simstack_model
class FileList(EmbeddedModel, ObjectListMixin[FileStack]):
    elements: List[ObjectId] = Field(default_factory=list)

    def __init__(self, **data):
        data, cache = self._normalize_elements_for_init(data)
        EmbeddedModel.__init__(self, **data)
        if cache is not None:
            self._set_cache(cache)

    def __iter__(self) -> Iterator[FileStack]:
        return ObjectListMixin.__iter__(self)


@simstack_model
class FileListModel(Model, ObjectListMixin[FileStack]):
    elements: List[ObjectId] = Field(default_factory=list)

    def __init__(self, **data):
        data, cache = self._normalize_elements_for_init(data)
        Model.__init__(self, **data)
        if cache is not None:
            self._set_cache(cache)

    def __iter__(self) -> Iterator[FileStack]:
        return ObjectListMixin.__iter__(self)


@simstack_model
class FileListIO(Model):
    file_list: FileList = Field(default_factory=FileList)
    task_status: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None

