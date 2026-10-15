"""Shared mechanics for scope-specific runner API clients."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Self, TypeVar

from pydantic import BaseModel, ValidationError

from simstack.runner_protocol import (
    LOG_BATCH_ROUTE,
    RunnerLogBatchRequestV1,
    RunnerLogBatchResponseV1,
    RunnerProtocolError,
    validate_runner_capabilities_v1,
)

from .config import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    RunnerApiSettings,
)
from .errors import RunnerApiConfigurationError, RunnerApiError
from .transport import RunnerHttpTransport

T = TypeVar("T")


class BaseRunnerClient:
    """Shared authenticated transport; endpoint methods live on scoped clients."""

    def __init__(
        self,
        *,
        server_url: str,
        runner_token: str,
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        if (
            not isinstance(server_url, str)
            or not server_url
            or server_url != server_url.strip()
        ):
            raise RunnerApiConfigurationError("SIMSTACK_SERVER_URL is required")
        if (
            not isinstance(runner_token, str)
            or not runner_token
            or runner_token != runner_token.strip()
        ):
            raise RunnerApiConfigurationError("SIMSTACK_RUNNER_TOKEN is required")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise RunnerApiConfigurationError(
                "Runner API timeout must be a positive integer"
            )
        if type(retry_attempts) is not int or retry_attempts < 0:
            raise RunnerApiConfigurationError(
                "Runner API retry attempts must be a non-negative integer"
            )
        if (
            isinstance(retry_backoff_seconds, bool)
            or not isinstance(retry_backoff_seconds, (int, float))
            or retry_backoff_seconds < 0
        ):
            raise RunnerApiConfigurationError(
                "Runner API retry backoff must be non-negative"
            )

        self.server_url = server_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._token_lock = threading.Lock()
        self._runner_token = runner_token
        self._transport = RunnerHttpTransport(
            server_url=self.server_url,
            token=lambda: self.runner_token,
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=self.retry_backoff_seconds,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        server_url: str | None = None,
        runner_token: str | None = None,
    ) -> Self:
        settings = RunnerApiSettings.from_environment(
            server_url=server_url,
            runner_token=runner_token,
        )
        return cls(
            server_url=settings.server_url,
            runner_token=settings.runner_token,
            timeout_seconds=settings.timeout_seconds,
            retry_attempts=settings.retry_attempts,
            retry_backoff_seconds=settings.retry_backoff_seconds,
        )

    @property
    def runner_token(self) -> str:
        with self._token_lock:
            return self._runner_token

    def _replace_token(self, token: str) -> None:
        if not isinstance(token, str) or not token or token != token.strip():
            raise RunnerApiError("Runner API returned an invalid rotated token")
        with self._token_lock:
            self._runner_token = token

    def _copy_payload_limit_to(self, client: "BaseRunnerClient") -> None:
        client._transport.max_payload_bytes = self._transport.max_payload_bytes

    @staticmethod
    def _protocol(call: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        try:
            return call(*args, **kwargs)
        except (RunnerProtocolError, ValidationError) as exc:
            raise RunnerApiError(str(exc)) from exc

    @classmethod
    def _wire_dto(
        cls,
        model: type[T],
        payload: Mapping[str, Any],
        *,
        operation: str,
    ) -> T:
        """Validate a JSON-shaped payload with the canonical strict DTO."""

        try:
            encoded = json.dumps(dict(payload), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise RunnerApiError(f"Runner {operation} is not valid JSON") from exc
        try:
            return model.model_validate_json(encoded, strict=True)  # type: ignore[attr-defined,no-any-return]
        except ValidationError as exc:
            raise RunnerApiError(
                f"Runner {operation} does not match the exact v1 DTO: {exc}"
            ) from exc

    @classmethod
    def _wire_body(
        cls,
        model: type[BaseModel],
        payload: Mapping[str, Any],
        *,
        operation: str,
        exclude_none: bool = True,
    ) -> dict[str, Any]:
        parsed = cls._wire_dto(model, payload, operation=operation)
        return parsed.model_dump(mode="json", exclude_none=exclude_none)

    async def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        retry_safe: bool = False,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._transport.request,
            method,
            path,
            payload,
            retry_safe=retry_safe,
        )

    async def download_workflow_archive(self, installation_id: str, destination: Path, *, revision: str, claim_id: str, attempt: int, task_id: str | None = None, max_bytes: int | None = None) -> None:
        from simstack.runner_protocol.paths import workflow_archive_path

        path = workflow_archive_path(installation_id, claim_id=claim_id, attempt=attempt, task_id=task_id)
        await asyncio.to_thread(self._transport.download, path, destination, sha256=revision, **({"max_bytes": max_bytes} if max_bytes is not None else {}))

    @staticmethod
    def _require_object(
        payload: dict[str, Any] | None,
        *,
        operation: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise RunnerApiError(f"Runner {operation} returned an empty response")
        return payload

    def _configure_capabilities(
        self,
        capabilities: Mapping[str, Any],
        *,
        operation: str,
    ) -> None:
        max_payload, _ = self._protocol(
            validate_runner_capabilities_v1,
            capabilities,
            operation=operation,
        )
        self._transport.max_payload_bytes = max_payload

    async def send_logs(
        self,
        *,
        logs: Sequence[Mapping[str, Any]] = (),
        events: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        body = self._log_body(logs=logs, events=events)
        response = self._require_object(
            await self._request("POST", LOG_BATCH_ROUTE, body),
            operation="runner log batch",
        )
        return self._wire_dto(
            RunnerLogBatchResponseV1,
            response,
            operation="runner log batch response",
        ).model_dump(mode="json")

    def send_logs_sync(
        self,
        *,
        logs: Sequence[Mapping[str, Any]] = (),
        events: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        body = self._log_body(logs=logs, events=events)
        response = self._require_object(
            self._transport.request("POST", LOG_BATCH_ROUTE, body),
            operation="runner log batch",
        )
        return self._wire_dto(
            RunnerLogBatchResponseV1,
            response,
            operation="runner log batch response",
        ).model_dump(mode="json")

    @staticmethod
    def _log_body(
        *,
        logs: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        return BaseRunnerClient._wire_body(
            RunnerLogBatchRequestV1,
            {
                "logs": [dict(entry) for entry in logs],
                "events": [dict(entry) for entry in events],
            },
            operation="runner log batch request",
        )
