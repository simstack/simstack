from pydantic import PlainSerializer
from typing_extensions import Annotated
import base64
from typing import Optional, List, TypeVar
from odmantic import Model, Field, EmbeddedModel
from simstack.models.parameters import Parameters
from simstack.models.pickle_models import FunctionPickle
import logging

logger = logging.getLogger("Models")

T = TypeVar("T")

B64Bytes = Annotated[
    bytes, PlainSerializer(lambda b: base64.b64encode(b).decode("ascii"), when_used="json")
]


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
    input_mappings: List[DataMapping]
    result_mappings: List[DataMapping] = Field(default_factory=list)
    called_nodes: List[str] = Field(default_factory=list)
    description: Optional[str] = ""
    favorite: bool = False
    default_parameters: Parameters
    pickle_function: Optional[
        FunctionPickle
    ] = None  # Reference to FunctionPickle if available


    model_config = {
        "collection": "node_model",
    }

    