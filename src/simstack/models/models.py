import base64
import logging
from typing import List, Optional, TypeVar

from odmantic import EmbeddedModel, Field, Model

from simstack.models.parameters import Parameters
from simstack.models.pickle_models import FunctionPickle
from simstack.models.workflow_repository import OptionalCodeSource

logger = logging.getLogger("Models")

T = TypeVar("T")


class ModelMapping(Model):
    """
    name: shorthand - must be unique
    mapping: full name - path relative to project root in module.module.class/function format
    """

    name: str = Field(unique=True)
    mapping: str = Field(unique=True)
    version: Optional[str] = None
    collection_name: str
    json_schema: Optional[str] = None
    ui_schema: Optional[str] = None
    code_source: OptionalCodeSource = None


class DataMapping(EmbeddedModel):
    name: str
    mapping: str
    description: Optional[str] = ""
    version: Optional[str] = None


class NodeModel(Model):
    name: str = Field(unique=True)
    function_mapping: str = Field(unique=True)
    version: Optional[str] = None
    input_mappings: List[DataMapping]
    result_mappings: List[DataMapping] = Field(default_factory=list)
    called_nodes: List[str] = Field(default_factory=list)
    description: Optional[str] = ""
    favorite: bool = False
    expose_in_submit: bool = True
    code_source: OptionalCodeSource = None
    default_parameters: Parameters
    # Reference to FunctionPickle if available.
    pickle_function: Optional[FunctionPickle] = None

    model_config = {
        "collection": "node_model",
        "json_encoders": {bytes: lambda b: base64.b64encode(b).decode("ascii")},
    }
