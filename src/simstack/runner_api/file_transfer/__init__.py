"""Scope-specific file-transfer API clients and safe path helpers."""

# This package intentionally re-exports its typed public facade.
# ruff: noqa: F401

from .client import ExecutionFileTransferClient, SupervisorFileTransferClient
from .transport import DownloadResult, FileTransferError
from .path_safety import (
    first_available_remote_location,
    path_for_file_instance,
    resolve_instance_path,
    resource_name,
    transfer_resource_name,
)

__all__ = [name for name in globals() if not name.startswith("_")]
