from datetime import datetime
from typing import Optional, List

from odmantic import Model, Field, ObjectId, Reference

from simstack.core.definitions import TaskStatus
from simstack.models.file_list import FileList
from simstack.models.parameters import Parameters
from simstack.models.named_data_reference import NamedDataReference

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
    :ivar input_references: List of references to input data.
    :type input_references: List[NamedDataReference]
    :ivar results_references: List of references to result data.
    :type results_references: List[NamedDataReference]
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
    version: Optional[str] = None
    # Keep this as Optional[ObjectId] instead of Reference(Project):
    # in the ODMantic version used here, nullable references are not supported
    # as Optional[Project] + Reference() field definitions.
    project: Optional[ObjectId] = Field(default=None)
    category: Optional[str] = None
    description: Optional[str] = None
    call_path: Optional[str] = None
    assignment_rule_id: Optional[str] = None
    assignment_rule_name: Optional[str] = None
    assignment_pattern: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None
    return_kind: Optional[str] = None

    input_references: List[NamedDataReference] = Field(default_factory=list)
    results_references: List[NamedDataReference] = Field(default_factory=list)

    info_files: FileList = Field(default_factory=FileList)

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

    def populate_results_from_duplicate(self, duplicate: "NodeRegistry"):
        self.results_references = duplicate.results_references
        self.info_files = duplicate.info_files
        self.status = duplicate.status
        self.completed_at = duplicate.completed_at
        self.job_id = duplicate.job_id
        self.artifact_ids = duplicate.artifact_ids
        self.error = duplicate.error
        self.message = duplicate.message
        self.return_kind = duplicate.return_kind
        if duplicate.custom_name is not None:
            self.custom_name = duplicate.custom_name

async def find_child_nodes(task_id: str) -> List[NodeRegistry]:
    from simstack.core.context import context
    return await context.db.find(NodeRegistry, {"parent_ids": {"$in": [task_id]}})
