import logging
import re
from datetime import datetime
from typing import Optional, Dict, Any

from pymongo import MongoClient

# Regular expression patterns
task_pattern = re.compile(r"task_id: ([\w\-\.]+)")
resource_pattern = re.compile(r"resource_id: ([\w\-\.]+)")


def extract_task_id(message: str) -> Optional[str]:
    """Extract task ID from log message."""
    match = task_pattern.search(message)
    return match.group(1) if match else None


def extract_resource_id(message: str) -> Optional[str]:
    """Extract resource ID from log message."""
    match = resource_pattern.search(message)
    return match.group(1) if match else None


class DBLogHandler(logging.Handler):
    """Logging handler that stores logs in MongoDB using pymongo."""

    def __init__(
        self,
        connection_string: str,
        db_name: str,
        collection_name: str = "logs",
    ):
        super().__init__()

        # Validate configuration
        if not connection_string or not isinstance(connection_string, str):
            raise ValueError("Invalid connection string")
        if not db_name or not isinstance(db_name, str):
            raise ValueError("Invalid database name")
        if not collection_name or not isinstance(collection_name, str):
            raise ValueError("Invalid collection name")

        # Store configuration
        self.connection_string = connection_string
        self.db_name = db_name
        self.collection_name = collection_name

        # Create MongoDB client
        self.client = MongoClient(self.connection_string)
        self.db = self.client[self.db_name]
        self.collection = self.db[self.collection_name]

    def emit(self, record: logging.LogRecord) -> None:
        """Process a log record by converting it to a log entry and inserting it into MongoDB."""
        try:
            # Create log entry as a dictionary
            log_entry = self.create_log_entry(record)

            # Insert the log entry into the collection
            self.collection.insert_one(log_entry)

        except Exception as e:
            # Use print instead of logging to avoid potential recursion
            print(f"Error in emit: {e}")
            self.handleError(record)

    def create_log_entry(self, record: logging.LogRecord) -> Dict[str, Any]:
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

    def close(self) -> None:
        """Close the MongoDB client connection."""
        try:
            if hasattr(self, "client") and self.client:
                self.client.close()
        except Exception as e:
            print(f"Error closing DB log handler client: {e}")

    def __del__(self):
        """Ensure connection is closed when handler is destroyed."""
        self.close()
