from typing import Optional, List

from pydantic import BaseModel, Field

from simstack.core.definitions import TaskStatus
from simstack.models.files import FileStack


class SimstackResult(BaseModel):
    """
    Represents the results from a Simstack operation.

    This class serves to encapsulate the status, messages, and files resulting
    from a Simstack operation. It provides structured attributes for the error
    message, operation message, and categorized files, ensuring clarity in the
    representation of these aspects. It also supports additional fields through
    flexible configuration.

    :ivar status: Indicates the status of the task, defaulting to `TaskStatus.COMPLETED`.
    :type status: TaskStatus
    :ivar error_message: Optionally stores an error message if the task encountered issues.
    :type error_message: Optional[str]
    :ivar message: An optional general message providing additional information about the task.
    :type message: Optional[str]
    :ivar files: A list of `FileStack` objects that represent the primary files involved in
        the operation. Defaults to an empty list.
    :type files: List[FileStack]
    :ivar info_files: A list of `FileStack` objects that provide additional or informational
        files pertaining to the operation. Defaults to an empty list. These files are shown in the results
        but not passed to the calling function
    :type info_files: List[FileStack]
    """

    status: TaskStatus = TaskStatus.COMPLETED
    error_message: Optional[str] = None
    message: Optional[str] = None
    files: List[FileStack] = Field(default_factory=list)
    info_files: List[FileStack] = Field(default_factory=list)

    class Config:
        extra = "allow"  # Allow extra fields without validation
