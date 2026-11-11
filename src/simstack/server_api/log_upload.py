from __future__ import annotations

import logging
import re
from datetime import datetime

from simstack.models.document.codec import encode_document_value

_TASK_ID_PATTERN = re.compile(r"task_id: ([\w\-\.]+)")
_RESOURCE_ID_PATTERN = re.compile(r"resource_id: ([\w\-\.]+)")


def extract_task_id(message: str) -> str | None:
    """Extract the first task identifier from a formatted log message."""
    match = _TASK_ID_PATTERN.search(message)
    return match.group(1) if match else None


def extract_resource_id(message: str) -> str | None:
    """Extract the first resource identifier from a formatted log message."""
    match = _RESOURCE_ID_PATTERN.search(message)
    return match.group(1) if match else None


class APILogHandler(logging.Handler):
    """Preserve the existing synchronous log-save behavior over HTTP."""

    def __init__(self, api, *, execution=None, resource=None, execution_prefix="/user-runtime/v3"):
        super().__init__()
        self.api = api
        self.execution = execution
        self.resource = resource
        self.execution_prefix = execution_prefix

    def emit(self, record):
        try:
            entry = self.create_log_entry(record)
            body = {"entries": encode_document_value([entry])}
            path = "/runner/logs"
            if self.execution is not None:
                from urllib.parse import quote
                entry["task_id"] = self.execution["task_id"]
                body = {"execution": self.execution, "entries": encode_document_value([entry])}
                path = f"{self.execution_prefix}/tasks/{self.execution['task_id']}/logs"
                if self.execution_prefix == "/user-runtime/v3":
                    path += "?resource=" + quote(self.resource, safe="")
            self.api.json_sync("POST", path, body)
        except Exception:
            self.handleError(record)

    def create_log_entry(self, record: logging.LogRecord) -> dict:
        # Format message
        message = self.format(record)

        # Extract task and resource IDs
        task_id = extract_task_id(message)
        resource_id = extract_resource_id(message)

        # Create log entry dictionary
        log_entry_dict = {
            "timestamp": datetime.fromtimestamp(record.created),
            "level": record.levelname,
            "logger_name": record.name,
            "message": message,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "task_id": task_id,
            "resource": resource_id,
            "thread_name": record.threadName,
            "process_name": record.processName,
        }

        # Add exception info if available
        if record.exc_info:
            log_entry_dict.update(
                {
                    "exception_type": record.exc_info[0].__name__
                    if record.exc_info and record.exc_info[0]
                    else None,
                    "exception_message": str(record.exc_info[1])
                    if record.exc_info and record.exc_info[1]
                    else None,
                    "exception_traceback": record.exc_text,
                }
            )

        return log_entry_dict


def setup_runner_logging(api, level="INFO", *, execution=None, resource=None, execution_prefix="/user-runtime/v3"):
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(
        "%(asctime)s - %(name)-15s - %(levelname)-10s - %(filename)-20s:%(lineno)4d - %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    uploader = APILogHandler(api, execution=execution, resource=resource, execution_prefix=execution_prefix)
    uploader.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(uploader)
    return root
