"""Backend-neutral encoding of structured records for the runner log protocol."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

task_pattern = re.compile(r"task_id: ([\w\-.]+)")
resource_pattern = re.compile(r"resource_id: ([\w\-.]+)")


def extract_task_id(message: str) -> str | None:
    match = task_pattern.search(message)
    return match.group(1) if match else None


def extract_resource_id(message: str) -> str | None:
    match = resource_pattern.search(message)
    return match.group(1) if match else None


def log_record_payload(
    record: logging.LogRecord,
    *,
    formatter: logging.Formatter | None = None,
) -> dict[str, Any]:
    """Build the JSON-safe payload used by the runner log API."""

    message = record.getMessage()
    payload: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).isoformat(),
        "level": record.levelname,
        "logger_name": record.name,
        "message": message,
        "module": record.module,
        "function": record.funcName,
        "line": record.lineno,
        "task_id": extract_task_id(message),
        "thread_name": record.threadName,
        "process_name": record.processName,
    }
    if record.exc_info:
        payload.update(
            {
                "exception_type": (
                    record.exc_info[0].__name__ if record.exc_info[0] else None
                ),
                "exception_message": str(record.exc_info[1]),
                "exception_traceback": (
                    formatter.formatException(record.exc_info)
                    if formatter is not None
                    else logging.Formatter().formatException(record.exc_info)
                ),
            }
        )
    return payload
