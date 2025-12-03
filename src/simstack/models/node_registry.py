from datetime import datetime
from typing import Optional, List

from odmantic import Model, Field, ObjectId, Reference

from simstack.core.definitions import TaskStatus
from simstack.models.file_list import FileList
from simstack.models.parameters import Parameters
from simstack.core.engine import current_engine_context


class NodeRegistry(Model):
    """
    Represents a registry for nodes with associated metadata, configurations, and status
    information. It allows tracking the state and attributes of a workflow node, including its
    execution parameters, process status, and relationship to other nodes.

    This class is designed for managing workflow instances and their lifecycle, with
    capabilities to monitor execution states, inputs, outputs, and associated timestamps.

    :ivar name: The name of the node.
    :type name: str
    :ivar custom_name: An optional custom name for the node.
    :type custom_name: Optional[str]
    :ivar status: TheTaskStatus of the node in string format.
    :type status: TaskStatus
    :ivar category: An optional category classification for the node.
    :type category: Optional[str]
    :ivar description: An optional description providing details about the node.
    :type description: Optional[str]
    :ivar input_tables: The names of the classes associated with this node's inputs.
    :type input_tables: List[str]
    :ivar input_ids: The unique identifier for the specific workflow instance providing the
                     inputs.
    :type input_ids: List[ObjectId]
    :ivar result_tables: list of names of the result class of the ode's outputs.
    :type result_tables: List[str]
    :ivar result_ids: List of ids for the results instance.
    :type result_ids: ListObjectId]
    :ivar parent_ids: A list of identifiers representing parent nodes associated with this node.
    :type parent_ids: List[ObjectId]
    :ivar created_at: The timestamp when the node was created.
    :type created_at: datetime
    :ivar started_at: An optional timestamp indicating when the execution of the node started.
    :type started_at: Optional[datetime]
    :ivar completed_at: An optional timestamp indicating when the execution of the node was
                        completed.
    :type completed_at: Optional[datetime]
    :ivar function_hash: A hash value representing the unique function executed by this node.
    :type function_hash: str
    :ivar arg_hash: A hash value representing the unique arguments passed to the function of
                    this node.
    :type arg_hash: str
    :ivar func_mapping: A mapping identifier associated with the function executed by this
                        node.
    :type func_mapping: str
    :ivar is_async: A boolean value indicating whether the node execution is asynchronous.
    :type is_async: bool
    :ivar parameters: Parameters associated with the node execution.
    :type parameters: Parameters
    :ivar call_path: An optional path indicating where the function is called from by concatenating the name of all nodes in the call stack.
    :type call_path: Optional[str]
    """

    name: str
    status: TaskStatus
    custom_name: Optional[str] = None
    project: Optional[str] = "default"
    category: Optional[str] = None
    description: Optional[str] = None
    call_path: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None

    input_names: List[str] = Field(default_factory=list)
    input_tables: List[str] = Field(
        default_factory=list
    )  # The model mappings the input classes
    input_ids: List[ObjectId] = Field(
        default_factory=list
    )  # The ID of the specific workflow instance
    result_tables: List[str] = Field(
        default_factory=list
    )  # The model mappings of the result classes
    result_ids: List[ObjectId] = Field(default_factory=list)
    result_names: List[str] = Field(default_factory=list)

    info_files: FileList = Field(default=FileList())

    parent_ids: List[ObjectId] = Field(default_factory=list)
    artifact_ids: List[ObjectId] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    job_id: Optional[str] = None
    function_hash: str
    arg_hash: str
    func_mapping: str
    is_async: bool = False
    parameters: Parameters = Reference()


async def find_child_nodes(task_id: str) -> List[NodeRegistry]:
    engine = current_engine_context.get()
    return await engine.find(NodeRegistry, {"parent_ids": {"$in": [task_id]}})
