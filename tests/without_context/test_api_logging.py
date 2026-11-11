import logging
import sys
from datetime import datetime
from unittest.mock import Mock

from simstack.models.document.codec import decode_document_value
from simstack.server_api.log_upload import APILogHandler
from simstack.util.sanitized_output import sanitized_tail


def test_api_logging_preserves_formatted_record_fields():
    api = Mock()
    handler = APILogHandler(api)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    record = logging.LogRecord(
        "workflow",
        logging.INFO,
        "/source/node.py",
        42,
        "task_id: task-1 resource_id: cluster-a %s",
        ("finished",),
        None,
        func="calculate",
    )
    record.created = 1234567890
    handler.emit(record)
    method, path, body = api.json_sync.call_args.args
    assert (method, path) == ("POST", "/runner/logs")
    entries = decode_document_value(body["entries"])
    assert entries == [
        {
            "timestamp": datetime.fromtimestamp(record.created),
            "level": "INFO",
            "logger_name": "workflow",
            "message": "workflow INFO task_id: task-1 resource_id: cluster-a finished",
            "module": "node",
            "function": "calculate",
            "line": 42,
            "task_id": "task-1",
            "resource": "cluster-a",
            "thread_name": record.threadName,
            "process_name": record.processName,
        }
    ]


def test_api_logging_keeps_exception_metadata_and_sanitized_message():
    api = Mock()
    handler = APILogHandler(api)
    handler.setFormatter(logging.Formatter("%(message)s"))
    secret = "mongodb://private-user:private-password@database:27017/workflows"
    try:
        raise ValueError("workflow failed")
    except ValueError:
        record = logging.LogRecord(
            "workflow",
            logging.ERROR,
            "node.py",
            5,
            sanitized_tail("task_id: task-2 error at " + secret, secret),
            (),
            sys.exc_info(),
            func="run",
        )
    handler.emit(record)
    entries = decode_document_value(api.json_sync.call_args.args[2]["entries"])
    entry = entries[0]
    assert entry["exception_type"] == "ValueError"
    assert entry["exception_message"] == "workflow failed"
    assert "ValueError: workflow failed" in entry["exception_traceback"]
    assert entry["task_id"] == "task-2"
    assert "ValueError: workflow failed" in entry["message"]
    assert secret not in entry["message"]
    assert "private-password" not in entry["message"]
