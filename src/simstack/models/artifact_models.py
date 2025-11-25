from typing import Optional, Dict, Any

from odmantic import Model, Field

from simstack.models.pickle_models import FunctionPickle
from simstack.models.simstack_model import simstack_model


# class ArtifactList(List):
#     """A list that knows how to update its parent Artifact when modified."""
#
#     def __init__(self, parent_artifact, key, initial_values=None):
#         super().__init__(initial_values or [])
#         self.parent_artifact = parent_artifact
#         self.key = key
#
#     def append(self, value):
#         super().append(value)
#         self.parent_artifact[self.key] = self  # Update the parent
#
#     def extend(self, values):
#         super().extend(values)
#         self.parent_artifact[self.key] = self  # Update the parent
#
#     # Add other list methods that modify the list
#     def __setitem__(self, index, value):
#         super().__setitem__(index, value)
#         self.parent_artifact[self.key] = self
#
#     def __delitem__(self, index):
#         super().__delitem__(index)
#         self.parent_artifact[self.key] = self
#
#     def insert(self, index, value):
#         super().insert(index, value)
#         self.parent_artifact[self.key] = self
#
#     def pop(self, index=-1):
#         value = super().pop(index)
#         self.parent_artifact[self.key] = self
#         return value
#
#     def remove(self, value):
#         super().remove(value)
#         self.parent_artifact[self.key] = self
#
#     def clear(self):
#         super().clear()
#         self.parent_artifact[self.key] = self
#
#     def sort(self, *args, **kwargs):
#         super().sort(*args, **kwargs)
#         self.parent_artifact[self.key] = self
#
#     def reverse(self):
#         super().reverse()
#         self.parent_artifact[self.key] = self


@simstack_model
class ArtifactModel(Model):
    name: str
    description: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    path: Optional[str] = None

    model_config = {
        "collection": "artifacts"  # All subclasses will use the same collection
    }


# class IntArtifactModel(ArtifactModel):
#     type: Literal["int"] = "int"  # Fixed value for this subclass
#     value: int
#
# class FloatArtifactModel(ArtifactModel):
#     type: Literal["float"] = "float"  # Fixed value for this subclass
#     value: float
#
# class StringArtifactModel(ArtifactModel):
#     type: Literal["string"] = "string"  # Fixed value for this subclass
#     value: str
#
# class ImageArtifactModel(ArtifactModel):
#     type: Literal["image"] = "image"  # Fixed value for this subclass
#     value: str  # the image is stored as a base64 string or a file path
#
# # Forward reference for type hints
# ArtifactType = Union[ArtifactModel, IntArtifactModel, FloatArtifactModel, StringArtifactModel, 'ArtifactModelList', ImageArtifactModel]
#
# # List subclass - can contain other items
# class ArtifactModelList(ArtifactModel):
#     type: Literal["list"] = "list"
#     items: List[ArtifactType] = Field(default_factory=list)  # List of Item objects


class ArtifactMapping(Model):
    """
    ArtifactsMapper is a mapping between the artifact and a node registry-path.
    The workflow executor passes a path of the type

    node1.node2.node4. ... .nodeN

    where node is the function name of the node

    Regex can maps this to the target path of the ArtifactsMapping, e.g. a path

    *.parent1.node

    it would map on all nodes with name node that have been directly called by a node with the name parent1.


    """

    name: str = Field(default="artifact", unique=True)
    regex_pattern: str = Field(default="")
    function_mapping: str = Field(default="")
    function_code: str = Field(default="")
    pickle_function: Optional[FunctionPickle] = None

    def set_values(self, other: "ArtifactMapping") -> "ArtifactMapping":
        self.name = other.name
        self.regex_pattern = other.regex_pattern
        self.function_mapping = other.function_mapping
        self.function_code = other.function_code
        self.pickle_function = other.pickle_function
        return self
