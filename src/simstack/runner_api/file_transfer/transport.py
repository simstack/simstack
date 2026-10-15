"""Standard-library JSON and streaming HTTP transport for file transfers."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlparse

from simstack.runner_protocol import API_ROOT

logger = logging.getLogger(__name__)


class FileTransferError(RuntimeError):
    """Transfer failure, optionally carrying the HTTP status for retry policy."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DownloadResult:
    """Integrity-checked file published by the streaming transport."""

    path: Path
    size_bytes: int
    checksum_sha256: str


class FileTransferTransport:
    chunk_size = 1024 * 1024

    def __init__(
        self,
        *,
        server_url: str,
        token_provider: Callable[[], str],
        timeout_seconds: int,
        retry_attempts: int,
        retry_backoff_seconds: float,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("File transfer request timeout must be positive and finite")
        if type(retry_attempts) is not int or retry_attempts < 0:
            raise ValueError("File transfer retry attempts must be a non-negative integer")
        if not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
            raise ValueError("File transfer retry backoff must be non-negative and finite")
        parsed = urlparse(server_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid SIMSTACK_SERVER_URL: {server_url!r}")
        self._parsed = parsed
        self._token_provider = token_provider
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_backoff_seconds = retry_backoff_seconds

    @property
    def runner_token(self) -> str:
        token = str(self._token_provider()).strip()
        if not token:
            raise FileTransferError("Runner token is unavailable")
        return token

    def request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        attempts = self.retry_attempts + 1 if retry_safe else 1
        for attempt_index in range(attempts):
            connection = self._connection()
            try:
                headers = self._auth_headers()
                if body is not None:
                    headers["Content-Type"] = "application/json"
                connection.request(
                    method,
                    self._path(path),
                    body=body,
                    headers=headers,
                )
                return self._parse_response(connection.getresponse())
            except (OSError, http.client.HTTPException, FileTransferError) as exc:
                retryable = not isinstance(exc, FileTransferError) or (
                    exc.status_code is not None
                    and (exc.status_code >= 500 or exc.status_code == 423)
                )
                if not retryable or attempt_index + 1 >= attempts:
                    raise
                time.sleep(self.retry_backoff_seconds * (attempt_index + 1))
            finally:
                connection.close()
        raise AssertionError("unreachable")

    @staticmethod
    def _check_upload_stop(stop_event: Event | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise FileTransferError("File transfer upload stopped for runner shutdown")

    def upload_file(
        self, path: str, source_path: Path, *, stop_event: Event | None = None
    ) -> dict[str, Any]:
        source_path = Path(source_path)
        if not source_path.is_file():
            raise FileTransferError(
                f"Transfer source file does not exist: {source_path}"
            )
        attempts = self.retry_attempts + 1
        for attempt_index in range(attempts):
            self._check_upload_stop(stop_event)
            try:
                return self._upload_file_once(path, source_path, stop_event=stop_event)
            except (OSError, http.client.HTTPException, FileTransferError) as exc:
                retryable = not isinstance(exc, FileTransferError) or (
                    exc.status_code is not None
                    and (exc.status_code >= 500 or exc.status_code == 423)
                )
                if not retryable or attempt_index + 1 >= attempts:
                    raise
                time.sleep(self.retry_backoff_seconds * (attempt_index + 1))
        raise AssertionError("unreachable")

    def _upload_file_once(
        self, path: str, source_path: Path, *, stop_event: Event | None = None
    ) -> dict[str, Any]:
        size_bytes = source_path.stat().st_size
        digest = hashlib.sha256()
        with source_path.open("rb") as source:
            while chunk := source.read(self.chunk_size):
                self._check_upload_stop(stop_event)
                digest.update(chunk)
        checksum = digest.hexdigest()
        raw_chunk = os.environ.get("SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES")
        try:
            upload_chunk_bytes = 8 * 1024 * 1024 if raw_chunk is None else int(raw_chunk)
        except ValueError as exc:
            raise ValueError("SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES must be an integer") from exc
        if upload_chunk_bytes <= 0:
            raise ValueError("SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES must be positive")
        floor = 256 * 1024
        minimum = floor if upload_chunk_bytes > floor else max(1, upload_chunk_bytes // 2)
        send_ranges = size_bytes > upload_chunk_bytes
        while True:
            self._check_upload_stop(stop_event)
            try:
                if not send_ranges or size_bytes == 0:
                    return self._put_upload(path, source_path, offset=0, length=size_bytes,
                                            total_size=size_bytes, checksum=checksum,
                                            include_content_range=False, stop_event=stop_event)
                result: dict[str, Any] = {}
                for offset in range(0, size_bytes, upload_chunk_bytes):
                    self._check_upload_stop(stop_event)
                    result = self._put_upload(path, source_path, offset=offset,
                                             length=min(upload_chunk_bytes, size_bytes - offset),
                                             total_size=size_bytes, checksum=checksum,
                                             include_content_range=True, stop_event=stop_event)
                return result
            except FileTransferError as exc:
                if exc.status_code != 413:
                    raise
                if not send_ranges:
                    send_ranges = True
                    upload_chunk_bytes = min(upload_chunk_bytes, 512 * 1024)
                    continue
                if upload_chunk_bytes // 2 < minimum:
                    raise
                upload_chunk_bytes //= 2

    def _put_upload(self, path: str, source_path: Path, *, offset: int, length: int,
                    total_size: int, checksum: str, include_content_range: bool,
                    stop_event: Event | None = None) -> dict[str, Any]:
        self._check_upload_stop(stop_event)
        connection = self._connection()
        try:
            connection.putrequest("PUT", self._path(path))
            for key, value in self._auth_headers().items():
                connection.putheader(key, value)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(length))
            connection.putheader("X-File-Size", str(total_size))
            connection.putheader("X-Checksum-SHA256", checksum)
            if include_content_range:
                connection.putheader("Content-Range", f"bytes {offset}-{offset + length - 1}/{total_size}")
            connection.endheaders()
            with source_path.open("rb") as handle:
                handle.seek(offset)
                remaining = length
                while remaining:
                    self._check_upload_stop(stop_event)
                    chunk = handle.read(min(self.chunk_size, remaining))
                    if not chunk:
                        raise FileTransferError("Transfer source file changed during upload")
                    connection.send(chunk)
                    remaining -= len(chunk)
            return self._parse_response(connection.getresponse())
        finally:
            connection.close()

    def download_file(self, path: str, target_path: Path) -> DownloadResult:
        for attempt_index in range(self.retry_attempts + 1):
            try:
                return self._download_file_once(path, target_path)
            except (OSError, http.client.HTTPException, FileTransferError) as exc:
                retryable = not isinstance(exc, (FileTransferError, FileExistsError)) or (
                    isinstance(exc, FileTransferError)
                    and exc.status_code is not None
                    and exc.status_code >= 500
                )
                if not retryable or attempt_index >= self.retry_attempts:
                    raise
                time.sleep(self.retry_backoff_seconds * (attempt_index + 1))
        raise AssertionError("unreachable")

    def _download_file_once(self, path: str, target_path: Path) -> DownloadResult:
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connection()
        temp_name: str | None = None
        try:
            connection.request(
                "GET",
                self._path(path),
                headers=self._auth_headers(),
            )
            response = connection.getresponse()
            if response.status >= 400:
                self._raise_response(response)
            raw_size = response.getheader("X-File-Size")
            expected_checksum = response.getheader("X-Checksum-SHA256")
            try:
                expected_size = int(raw_size) if raw_size is not None else -1
            except ValueError as exc:
                raise FileTransferError("Download has invalid immutable size metadata") from exc
            if (
                expected_size < 0
                or expected_checksum is None
                or re.fullmatch(r"[0-9a-fA-F]{64}", expected_checksum) is None
            ):
                raise FileTransferError("Download has no valid immutable size/checksum metadata")

            sha256 = hashlib.sha256()
            size_bytes = 0
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(target_path.parent),
                prefix=f".{target_path.name}.",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                while chunk := response.read(self.chunk_size):
                    if size_bytes + len(chunk) > expected_size:
                        raise FileTransferError("Download exceeds immutable source size")
                    temp_file.write(chunk)
                    sha256.update(chunk)
                    size_bytes += len(chunk)
            checksum = sha256.hexdigest()
            if size_bytes != expected_size or checksum != expected_checksum.lower():
                raise FileTransferError("Downloaded bytes do not match immutable source metadata")
            # Publishing is exclusive and atomic: failed/corrupt downloads never
            # become visible, and neither a concurrent file nor a symlink is replaced.
            os.link(temp_name, target_path)
            Path(temp_name).unlink()
            return DownloadResult(
                path=target_path,
                size_bytes=size_bytes,
                checksum_sha256=checksum,
            )
        except Exception:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to clean temporary download %s", temp_name)
            raise
        finally:
            connection.close()

    def _connection(self) -> http.client.HTTPConnection:
        connection_type = (
            http.client.HTTPSConnection
            if self._parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_type(self._parsed.netloc, timeout=self.timeout_seconds)

    def _path(self, path: str) -> str:
        base_path = self._parsed.path.rstrip("/")
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            return parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if not path.startswith("/"):
            path = f"/{path}"
        if base_path.endswith(API_ROOT) and path.startswith(f"{API_ROOT}/"):
            path = path[len(API_ROOT) :]
        return f"{base_path}{path}" if base_path else path

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.runner_token}"}

    def _parse_response(self, response: http.client.HTTPResponse) -> dict[str, Any]:
        if response.status >= 400:
            self._raise_response(response)
        payload = response.read()
        if not payload:
            return {}
        if "application/json" not in (response.getheader("Content-Type") or ""):
            raise FileTransferError("File transfer API returned non-JSON metadata")
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FileTransferError("File transfer API returned invalid JSON metadata") from exc
        if not isinstance(parsed, dict):
            raise FileTransferError("File transfer API returned a non-object response")
        return parsed

    @staticmethod
    def _raise_response(response: http.client.HTTPResponse) -> None:
        payload = response.read()
        detail = (
            payload.decode("utf-8", errors="replace") if payload else response.reason
        )
        raise FileTransferError(
            f"File transfer API returned HTTP {response.status}: {detail}",
            status_code=response.status,
        )
