from datetime import datetime
from typing import Optional, List

from simstack.models.document import Field, Model, ObjectId
from pydantic import ConfigDict

from simstack.models.parameters import Resource
from simstack.runner_protocol.constants import RunnerEventType, RunnerProcessType

RunnerEventEnum = RunnerEventType
RunnerType = RunnerProcessType


class RunnerEvent(Model):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    timestamp: datetime = Field(default_factory=datetime.now)
    event: RunnerEventEnum
    resource: Resource
    runner_type: RunnerType
    hostname: Optional[str] = None
    user: Optional[str] = None
    pid: Optional[int] = None
    runner_version: Optional[str] = None
    node_id: Optional[ObjectId] = None
    message: Optional[str] = None
    git_status: List[str] = Field(default_factory=list)
