import base64
from typing import Optional, List, TypeVar
from odmantic import Model, Field, EmbeddedModel
from simstack.models.parameters import Parameters
from simstack.models.pickle_models import FunctionPickle
from pydantic import model_validator, field_validator
from typing import Any
import logging

logger = logging.getLogger("Models")

def fix_list(v: Any) -> list:
    if v is None:
        return []
    if not isinstance(v, list):
        return v
    return [item for item in v if item is not None]

T = TypeVar("T")


class ModelMapping(Model):
    """
    name: shorthand - must be unique
    mapping: full name - path relative to project root in module.module.class/function format
    """
    name: str = Field(unique=True)
    mapping: str = Field(unique=True)
    collection_name: str
    json_schema: Optional[str] = None
    ui_schema: Optional[str] = None


class DataMapping(EmbeddedModel):
    name: str
    mapping: str
    description: Optional[str] = ""

class NodeModel(Model):
    name: str = Field(unique=True)
    function_mapping: str = Field(unique=True)
    input_mappings: List[DataMapping] = Field(default_factory=list)
    result_mappings: List[DataMapping] = Field(default_factory=list)
    called_nodes: List[str] = Field(default_factory=list)
    description: Optional[str] = ""
    favorite: bool = False
    default_parameters: Parameters
    pickle_function: Optional[
        FunctionPickle
    ] = None  # Reference to FunctionPickle if available

    @model_validator(mode="before")
    @classmethod
    def validate_lists_before(cls, data):
        if isinstance(data, dict):
            for field in ["input_mappings", "result_mappings"]:
                if field in data:
                    data[field] = fix_list(data[field])
                else:
                    data[field] = []
            if "called_nodes" in data:
                data["called_nodes"] = fix_list(data["called_nodes"])
            else:
                data["called_nodes"] = []
        return data

    @model_validator(mode="after")
    def ensure_lists_not_none(self) -> "NodeModel":
        if self.input_mappings is None:
            self.input_mappings = []
        if self.result_mappings is None:
            self.result_mappings = []
        if self.called_nodes is None:
            self.called_nodes = []
        return self

    model_config = {
        "collection": "node_model",
        "json_encoders": {bytes: lambda b: base64.b64encode(b).decode("ascii")},
    }

    