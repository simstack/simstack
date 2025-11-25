from datetime import datetime
from enum import Enum
from typing import Optional, List

from odmantic import Model, ObjectId, Field
from pydantic import ConfigDict

from simstack.models.parameters import Resource


class RunnerEventEnum(str, Enum):
    SHUTDOWN = "shutdown"
    RUNNER_STARTED = "runner_started"
    NODE_STARTED = "node_started"
    NODE_SUBMIT = "node_submit"
    ALIVE = "alive"
    RESTART = "restart"
    CRONTAB_OK = "crontab_ok"
    CRONTAB_GONE = "crontab_gone"


class RunnerType(str, Enum):
    NODE_RUNNER = "node_runner"
    RESOURCE_RUNNER = "resource_runner"


class RunnerEvent(Model):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    timestamp: datetime = Field(default_factory=datetime.now)
    event: RunnerEventEnum
    resource: Resource
    runner_type: RunnerType
    hostname: Optional[str] = None
    user: Optional[str] = None
    pid: Optional[int] = None
    node_id: Optional[ObjectId] = None
    message: Optional[str] = None
    git_status: List[str] = Field(default_factory=list)
