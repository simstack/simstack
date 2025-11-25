from datetime import datetime
from enum import Enum
from typing import Optional

from odmantic import Model, Field

from simstack.models import Parameters


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogEntry(Model):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    level: LogLevel  # Use enum for validation
    logger_name: str
    message: str
    module: str
    function: str
    line: int
    task_id: Optional[str] = None
    resource: Optional[str] = None
    thread_name: Optional[str] = None
    process_name: Optional[str] = None
    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    exception_traceback: Optional[str] = None
    # extra_data: Dict[str, Any] = Field(default_factory=dict)
    parameters: Optional[Parameters] = None  # If Parameters is related to LogEntry

    # Replace Config class with model_config
    model_config = {"collection": "logs"}
