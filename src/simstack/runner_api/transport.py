from __future__ import annotations

import asyncio
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class RunnerAPIError(RuntimeError):
    """An API request failed; this is not a scientific calculation failure."""


class RunnerAPIClient:
    """Retry safe reads; writes are never repeated after an ambiguous failure."""

    def __init__(self, server_url: str, token: str, *, timeout: float = 60,
                 read_attempts: int = 3, read_retry_delay: float = 1):
        parsed = urllib.parse.urlsplit(server_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Runner server URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Runner server URL cannot contain credentials or a query")
        if not token:
            raise ValueError("Runner API token is required")
        if read_attempts < 1 or read_retry_delay < 0:
            raise ValueError("Read attempts must be positive and retry delay non-negative")
        self.server_url = server_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.read_attempts = read_attempts
        self.read_retry_delay = read_retry_delay

    @property
    def token(self):
        from simstack.runner_api.credentials import current_token
        return current_token(self.server_url, self._token)

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("SIMSTACK_SERVER_URL", ""),
            os.environ.get("SIMSTACK_RUNNER_TOKEN")
            or os.environ.get("SIMSTACK_SERVER_TOKEN", ""),
        )

    @staticmethod
    def _is_read(method: str, path: str) -> bool:
        return method.upper() == "GET" or (
            method.upper() == "POST" and path == "/runner/data/query"
        )

    def json_sync(self, method: str, path: str, body=None):
        attempts = self.read_attempts if self._is_read(method, path) else 1
        for attempt in range(attempts):
            try:
                return self._request_json(method, path, body)
            except (OSError, http.client.IncompleteRead) as exc:
                self._handle_error(exc, can_retry=attempt + 1 < attempts)
                time.sleep(self.read_retry_delay * 2 ** attempt)

    async def json(self, method: str, path: str, body=None):
        attempts = self.read_attempts if self._is_read(method, path) else 1
        for attempt in range(attempts):
            try:
                return await asyncio.to_thread(self._request_json, method, path, body)
            except (OSError, http.client.IncompleteRead) as exc:
                self._handle_error(exc, can_retry=attempt + 1 < attempts)
                await asyncio.sleep(self.read_retry_delay * 2 ** attempt)

    @staticmethod
    def _handle_error(exc, *, can_retry: bool):
        if isinstance(exc, urllib.error.HTTPError):
            if can_retry and 500 <= exc.code < 600:
                exc.close()
                return
            try:
                detail = json.loads(exc.read()).get("detail", "Runner API request failed")
            except (ValueError, AttributeError, OSError, http.client.IncompleteRead):
                detail = "Runner API request failed"
            finally:
                exc.close()
            raise RunnerAPIError(f"Runner API returned HTTP {exc.code}: {detail}") from None

        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if can_retry and isinstance(reason, (TimeoutError, ConnectionError, http.client.IncompleteRead)):
            return
        raise RunnerAPIError("Runner API is unavailable; write outcome may be indeterminate") from exc

    def _request_json(self, method: str, path: str, body=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Runner API paths must be relative to the configured server")
        request = urllib.request.Request(
            self.server_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as rejected:
            if rejected.code != 401:
                raise
            from simstack.runner_api.credentials import refreshed_token
            replacement = refreshed_token(self.server_url, request.get_header("Authorization")[7:])
            if replacement is None:
                raise
            # A rejected authorization has not dispatched the operation.
            rejected.close()
            request.add_header("Authorization", "Bearer " + replacement)
            response = urllib.request.urlopen(request, timeout=self.timeout)
        with response:
            return json.load(response)


def runner_environment(environment=None) -> dict[str, str]:
    """Propagate API credentials without inheriting database configuration."""
    result = dict(os.environ if environment is None else environment)
    for key in list(result):
        upper = key.upper()
        if upper.startswith("SIMSTACK_DB_") or upper in {
            "CONNECTION_STRING",
            "MONGODB_URI",
            "MONGO_URI",
            "MONGO_URL",
            "DATABASE_URL",
            "DATABASE_NAME",
            "DB_NAME",
            "DB_URI",
        }:
            result.pop(key, None)
    return result
