"""Blocking HTTP transport isolated from runner protocol and orchestration logic."""

from __future__ import annotations

import http.client
import json
import time
import hashlib
from pathlib import Path
import tempfile
from collections.abc import Callable, Mapping
from typing import Any, cast
from urllib.parse import ParseResult, urlparse

from simstack.runner_protocol import API_ROOT, RUNNER_API_PREFIX

from .errors import RunnerApiConfigurationError, RunnerApiError


class RunnerHttpTransport:
    """Small JSON-over-HTTP transport with bounded, opt-in retries."""

    def __init__(
        self,
        *,
        server_url: str,
        token: Callable[[], str],
        timeout_seconds: int,
        retry_attempts: int,
        retry_backoff_seconds: float,
    ) -> None:
        parsed = urlparse(server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RunnerApiConfigurationError(
                f"Invalid SIMSTACK_SERVER_URL: {server_url!r}"
            )
        if parsed.params or parsed.query or parsed.fragment:
            raise RunnerApiConfigurationError(
                "SIMSTACK_SERVER_URL must not contain params, a query, or a fragment"
            )
        self._parsed: ParseResult = parsed
        self._token = token
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_payload_bytes: int | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        *,
        retry_safe: bool = False,
    ) -> dict[str, Any] | None:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        if (
            body is not None
            and self.max_payload_bytes is not None
            and len(body) > self.max_payload_bytes
        ):
            raise RunnerApiError(
                "Runner API request exceeds server-advertised payload capacity"
            )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token()}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        attempts = self.retry_attempts + 1 if retry_safe else 1
        for attempt_index in range(attempts):
            connection = self.connection()
            try:
                connection.request(method, self.path(path), body=body, headers=headers)
                response = connection.getresponse()
                raw = (
                    response.read()
                    if self.max_payload_bytes is None
                    else response.read(self.max_payload_bytes + 1)
                )
                if self.max_payload_bytes is not None and len(raw) > self.max_payload_bytes:
                    raise RunnerApiError("Runner API response exceeds server-advertised payload policy")
            except (OSError, http.client.HTTPException) as exc:
                if attempt_index + 1 < attempts:
                    time.sleep(self.retry_backoff_seconds * (attempt_index + 1))
                    continue
                raise RunnerApiError(f"Runner API request failed: {exc}") from exc
            finally:
                connection.close()
            if (
                retry_safe
                and 500 <= response.status < 600
                and attempt_index + 1 < attempts
            ):
                time.sleep(self.retry_backoff_seconds * (attempt_index + 1))
                continue
            break

        if response.status >= 400:
            detail = raw.decode("utf-8", errors="replace") if raw else response.reason
            raise RunnerApiError(
                f"Runner API returned HTTP {response.status}: {detail}",
                status_code=response.status,
            )
        if response.status == 204 or not raw:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerApiError("Runner API returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise RunnerApiError("Runner API returned a non-object JSON response")
        return cast(dict[str, Any], parsed)

    def connection(self) -> http.client.HTTPConnection:
        connection_cls = (
            http.client.HTTPSConnection
            if self._parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_cls(self._parsed.netloc, timeout=self.timeout_seconds)

    def download(self, path: str, destination: Path, *, sha256: str, max_bytes: int | None = None) -> None:
        """Stream an authenticated same-origin artifact and publish only verified bytes."""
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise RunnerApiError("Workflow archive digest must be canonical SHA256")
        from simstack.workflows.limits import DEFAULT_INSTALLATION_LIMITS
        max_bytes = DEFAULT_INSTALLATION_LIMITS.max_archive_bytes if max_bytes is None else max_bytes
        if type(max_bytes) is not int or max_bytes <= 0:
            raise RunnerApiError("Workflow archive byte budget must be positive")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = self.connection()
        temporary: Path | None = None
        try:
            connection.request("GET", self.path(path), headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/zip"})
            response = connection.getresponse()
            if response.status != 200:
                raise RunnerApiError(f"Workflow archive returned HTTP {response.status}", status_code=response.status)
            digest = hashlib.sha256()
            consumed = 0
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".archive-", delete=False) as stream:
                temporary = Path(stream.name)
                while chunk := response.read(1024 * 1024):
                    consumed += len(chunk)
                    if consumed > max_bytes:
                        raise RunnerApiError("Workflow archive exceeds configured compressed size")
                    digest.update(chunk)
                    stream.write(chunk)
            if digest.hexdigest() != sha256:
                raise RunnerApiError("Workflow archive checksum does not match its pinned revision")
            temporary.replace(destination)
            temporary = None
        except (OSError, http.client.HTTPException) as exc:
            raise RunnerApiError(f"Workflow artifact download failed: {exc}") from exc
        finally:
            connection.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def path(self, path: str) -> str:
        if path.startswith(f"{API_ROOT}/"):
            return path
        relative = path if path.startswith("/") else f"/{path}"
        base_path = self._parsed.path.rstrip("/")
        if base_path.endswith(API_ROOT):
            api_root = base_path
        else:
            api_root = f"{base_path}{API_ROOT}" if base_path else API_ROOT
        runner_prefix = RUNNER_API_PREFIX.removeprefix(API_ROOT)
        return f"{api_root}{runner_prefix}{relative}"
