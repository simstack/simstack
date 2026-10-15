"""Scope-specific file-transfer clients over the shared wire protocol."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from simstack.runner_protocol import (
    FILE_TRANSFER_COLLECTION_ROUTE,
    FILE_TRANSFER_READY_STATUSES,
    FILE_TRANSFER_TERMINAL_FAILURE_STATUSES,
    FileTransferCompleteRequestV1,
    FileTransferCompleteResponseV1,
    FileTransferCreateRequestV1,
    FileTransferCreateResponseV1,
    FileTransferFailRequestV1,
    FileTransferListResponseV1,
    FileTransferRequestType,
    FileTransferRole,
    FileTransferStatus,
    FileTransferStatusResponseV1,
    FileTransferUploadResponseV1,
    file_transfer_complete_path,
    file_transfer_download_path,
    file_transfer_fail_path,
    file_transfer_restart_path,
    file_transfer_list_path,
    file_transfer_path,
    file_transfer_upload_path,
)

from ..config import (
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    FileTransferApiSettings,
)
from .transport import DownloadResult, FileTransferError, FileTransferTransport

DtoT = TypeVar("DtoT", bound=BaseModel)


class _BaseFileTransferClient:
    def __init__(
        self,
        *,
        server_url: str,
        runner_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        timeout_seconds: int,
        transfer_timeout_seconds: int,
        poll_seconds: float,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        if (runner_token is None) == (token_provider is None):
            raise ValueError(
                "File transfer client requires exactly one token or token provider"
            )
        if token_provider is None:
            static_token = str(runner_token or "").strip()
            if not static_token:
                raise ValueError("Runner token is required")

            def static_token_provider() -> str:
                return static_token

            token_provider = static_token_provider
        if (
            not math.isfinite(transfer_timeout_seconds)
            or not math.isfinite(poll_seconds)
            or transfer_timeout_seconds <= 0
            or poll_seconds <= 0
        ):
            raise ValueError("File transfer lifecycle intervals must be positive and finite")
        self.server_url = server_url.rstrip("/")
        self.transfer_timeout_seconds = transfer_timeout_seconds
        self.poll_seconds = poll_seconds
        self._transport = FileTransferTransport(
            server_url=self.server_url,
            token_provider=token_provider,
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
        )

    @property
    def runner_token(self) -> str:
        return self._transport.runner_token

    @staticmethod
    def _dto(model: type[DtoT], payload: dict[str, Any], *, operation: str) -> DtoT:
        try:
            encoded = json.dumps(payload, separators=(",", ":"))
            return model.model_validate_json(encoded, strict=True)
        except (TypeError, ValueError, ValidationError) as exc:
            raise FileTransferError(
                f"File transfer {operation} does not match the exact v1 DTO: {exc}"
            ) from exc

    @classmethod
    def _body(
        cls, model: type[DtoT], payload: dict[str, Any], *, operation: str
    ) -> dict[str, Any]:
        return cls._dto(model, payload, operation=operation).model_dump(
            mode="json", exclude_none=True
        )

    def _get_transfer(self, transfer_id: str) -> dict[str, Any]:
        response = self._transport.request_json(
            "GET", file_transfer_path(transfer_id), retry_safe=True
        )
        return self._dto(
            FileTransferStatusResponseV1,
            response,
            operation="status response",
        ).model_dump(mode="json")

    def _fail_transfer(
        self, transfer_id: str, *, error_message: str, error_code: str | None
    ) -> dict[str, Any]:
        body = self._body(
            FileTransferFailRequestV1,
            {"error_message": error_message, "error_code": error_code},
            operation="failure request",
        )
        response = self._transport.request_json(
            "POST", file_transfer_fail_path(transfer_id), body
        )
        return self._dto(
            FileTransferStatusResponseV1,
            response,
            operation="failure response",
        ).model_dump(mode="json")


class SupervisorFileTransferClient(_BaseFileTransferClient):
    """Operations authorized for a resource-scoped supervisor token."""

    @classmethod
    def from_context(cls) -> "SupervisorFileTransferClient":
        from simstack.core.context import context
        from simstack.runner_api.supervisor import SupervisorRunnerClient

        runner_api = _context_runner_api(context, SupervisorRunnerClient)
        return cls._from_runner_api(runner_api)

    @classmethod
    def _from_runner_api(cls, runner_api: Any) -> "SupervisorFileTransferClient":
        settings = FileTransferApiSettings.from_environment()
        return cls(
            server_url=runner_api.server_url,
            token_provider=lambda: runner_api.runner_token,
            timeout_seconds=settings.request_timeout_seconds,
            transfer_timeout_seconds=settings.transfer_timeout_seconds,
            poll_seconds=settings.poll_seconds,
        )

    def list_transfers(
        self,
        *,
        role: FileTransferRole = FileTransferRole.SOURCE,
        status: FileTransferStatus | None = FileTransferStatus.CREATED,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        response = self._transport.request_json(
            "GET",
            file_transfer_list_path(role=role, status=status, limit=limit),
            retry_safe=True,
        )
        parsed = self._dto(
            FileTransferListResponseV1,
            response,
            operation="list response",
        )
        return [item.model_dump(mode="json") for item in parsed.transfers]

    def upload_file(
        self, transfer_id: str, path: Path, *, stop_event: Event | None = None
    ) -> dict[str, Any]:
        response = self._transport.upload_file(
            file_transfer_upload_path(transfer_id), path, stop_event=stop_event
        )
        return self._dto(
            FileTransferUploadResponseV1,
            response,
            operation="upload response",
        ).model_dump(mode="json")

    def fail_transfer(
        self, transfer_id: str, *, error_message: str, error_code: str | None = None
    ) -> dict[str, Any]:
        return self._fail_transfer(
            transfer_id, error_message=error_message, error_code=error_code
        )


class ExecutionFileTransferClient(_BaseFileTransferClient):
    """Operations authorized for a claim-bound execution token."""

    @classmethod
    def from_context(cls) -> "ExecutionFileTransferClient":
        from simstack.core.context import context
        from simstack.runner_api.execution import ExecutionRunnerClient

        runner_api = _context_runner_api(context, ExecutionRunnerClient)
        return cls._from_runner_api(runner_api)

    @classmethod
    def _from_runner_api(cls, runner_api: Any) -> "ExecutionFileTransferClient":
        settings = FileTransferApiSettings.from_environment()
        return cls(
            server_url=runner_api.server_url,
            token_provider=lambda: runner_api.runner_token,
            timeout_seconds=settings.request_timeout_seconds,
            transfer_timeout_seconds=settings.transfer_timeout_seconds,
            poll_seconds=settings.poll_seconds,
        )

    def create_transfer(
        self,
        *,
        task_id: str,
        claim_id: str,
        attempt: int,
        file_stack_id: str,
        source_file_instance_id: str,
        source_resource_name: str,
        target_resource_name: str,
    ) -> dict[str, Any]:
        fingerprint_values = {
            "task_id": task_id,
            "claim_id": claim_id,
            "attempt": attempt,
            "file_stack_id": file_stack_id,
            "source_file_instance_id": source_file_instance_id,
            "source_resource_name": source_resource_name,
            "target_resource_name": target_resource_name,
            "request_type": FileTransferRequestType.RUNNER_TO_RUNNER,
        }
        request_id = hashlib.sha256(
            json.dumps(
                fingerprint_values, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        body = self._body(
            FileTransferCreateRequestV1,
            {**fingerprint_values, "request_id": request_id},
            operation="create request",
        )
        response = self._transport.request_json(
            "POST", FILE_TRANSFER_COLLECTION_ROUTE, body, retry_safe=True
        )
        return self._dto(
            FileTransferCreateResponseV1,
            response,
            operation="create response",
        ).model_dump(mode="json")

    def get_transfer(self, transfer_id: str) -> dict[str, Any]:
        return self._get_transfer(transfer_id)

    def restart_transfer(self, transfer_id: str) -> dict[str, Any]:
        response = self._transport.request_json(
            "POST", file_transfer_restart_path(transfer_id), retry_safe=True
        )
        return self._dto(
            FileTransferCreateResponseV1, response, operation="restart response"
        ).model_dump(mode="json")

    def wait_until_uploaded(
        self,
        transfer_id: str,
        *,
        timeout_seconds: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else self.transfer_timeout_seconds
        )
        interval = float(
            poll_interval_seconds
            if poll_interval_seconds is not None
            else self.poll_seconds
        )
        if not math.isfinite(timeout) or not math.isfinite(interval) or timeout <= 0 or interval <= 0:
            raise ValueError("File transfer lifecycle intervals must be positive and finite")
        deadline = time.monotonic() + timeout
        last_status: FileTransferStatus | str = "unknown"
        while time.monotonic() < deadline:
            transfer = self._get_transfer(transfer_id)
            last_status = FileTransferStatus(transfer["status"])
            if last_status in FILE_TRANSFER_READY_STATUSES:
                return transfer
            if last_status in FILE_TRANSFER_TERMINAL_FAILURE_STATUSES:
                raise FileTransferError(
                    f"Transfer {transfer_id} failed with status {last_status}: "
                    f"{transfer.get('error_message')}"
                )
            time.sleep(interval)
        raise FileTransferError(
            f"Timed out waiting for transfer {transfer_id} to upload; "
            f"last status was {last_status}."
        )

    def download_file(self, transfer_id: str, target_path: Path) -> DownloadResult:
        return self._transport.download_file(
            file_transfer_download_path(transfer_id), target_path
        )

    def complete_transfer(
        self,
        *,
        transfer_id: str,
        target_resource_name: str,
        target_path: str,
        size_bytes: int,
        checksum_sha256: str,
    ) -> dict[str, Any]:
        body = self._body(
            FileTransferCompleteRequestV1,
            {
                "target_resource_name": target_resource_name,
                "target_path": target_path,
                "size_bytes": size_bytes,
                "checksum_sha256": checksum_sha256,
            },
            operation="completion request",
        )
        response = self._transport.request_json(
            "POST",
            file_transfer_complete_path(transfer_id),
            body,
            retry_safe=True,
        )
        return self._dto(
            FileTransferCompleteResponseV1,
            response,
            operation="completion response",
        ).model_dump(mode="json")

    def fail_transfer(
        self, transfer_id: str, *, error_message: str, error_code: str | None = None
    ) -> dict[str, Any]:
        return self._fail_transfer(
            transfer_id, error_message=error_message, error_code=error_code
        )


def _context_runner_api(context: Any, expected_type: type[Any]) -> Any:
    if not context.runner_mode:
        raise FileTransferError(
            "File transfer requires initialized API-only runner mode"
        )
    runner_api = context.runner_api
    if not isinstance(runner_api, expected_type):
        raise FileTransferError(
            f"File transfer requires {expected_type.__name__} token scope"
        )
    return runner_api
