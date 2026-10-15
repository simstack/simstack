"""Exact runner file-transfer DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ..constants import FileTransferRequestType, FileTransferStatus
from .base import CanonicalString, StrictRunnerDto


class FileTransferCreateRequestV1(StrictRunnerDto):
    request_id: CanonicalString = Field(max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    task_id: CanonicalString
    claim_id: CanonicalString = Field(max_length=200)
    attempt: int = Field(ge=1)
    file_stack_id: CanonicalString
    source_file_instance_id: CanonicalString = Field(max_length=500)
    source_resource_name: CanonicalString = Field(max_length=500)
    target_resource_name: CanonicalString = Field(max_length=500)
    request_type: Literal[FileTransferRequestType.RUNNER_TO_RUNNER]


class FileTransferCreateResponseV1(StrictRunnerDto):
    transfer_id: CanonicalString
    status: FileTransferStatus
    source_upload_url: CanonicalString
    target_download_url: CanonicalString
    expires_at: datetime


class FileTransferStatusResponseV1(StrictRunnerDto):
    transfer_id: CanonicalString
    status: FileTransferStatus
    error_message: str | None
    error_code: str | None
    size_bytes: int | None = Field(ge=0)
    checksum_sha256: str | None = Field(pattern=r"^[0-9a-fA-F]{64}$")
    expires_at: datetime
    file_instance_id: CanonicalString | None = None
    target_path: CanonicalString | None = None


class FileTransferListItemV1(FileTransferStatusResponseV1):
    file_stack_id: CanonicalString
    source_file_instance_id: str | None
    source_resource_name: CanonicalString
    source_path: str | None
    target_resource_name: CanonicalString
    request_type: FileTransferRequestType


class FileTransferListResponseV1(StrictRunnerDto):
    transfers: list[FileTransferListItemV1]


class FileTransferUploadResponseV1(StrictRunnerDto):
    transfer_id: CanonicalString
    status: FileTransferStatus
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class FileTransferCompleteRequestV1(StrictRunnerDto):
    target_resource_name: CanonicalString = Field(max_length=500)
    target_path: CanonicalString = Field(max_length=4_000)
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class FileTransferCompleteResponseV1(StrictRunnerDto):
    transfer_id: CanonicalString
    status: Literal[FileTransferStatus.COMPLETED]
    file_instance_id: CanonicalString


class FileTransferFailRequestV1(StrictRunnerDto):
    error_message: CanonicalString = Field(max_length=100_000)
    error_code: str | None = Field(default=None, max_length=500)
