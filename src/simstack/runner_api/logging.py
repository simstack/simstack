from __future__ import annotations

import logging

from bson import json_util

from simstack.util.db_logger import DBLogHandler


class APILogHandler(logging.Handler):
    """Preserve the existing synchronous log-save behavior over HTTP."""

    def __init__(self, api):
        super().__init__()
        self.api = api

    def emit(self, record):
        try:
            # Reuse the exact document shape and exception metadata. No Mongo
            # handler is constructed, so no MongoDB client is opened here.
            entry = DBLogHandler.create_log_entry(self, record)
            self.api.json_sync(
                "POST", "/runner/logs", {"entries": json_util.dumps([entry])}
            )
        except Exception:
            self.handleError(record)


def setup_runner_logging(api, level="INFO"):
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
    uploader = APILogHandler(api)
    uploader.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(uploader)
    return root
