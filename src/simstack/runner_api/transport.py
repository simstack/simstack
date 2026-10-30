from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from bson import json_util
from pymongo.errors import DuplicateKeyError, OperationFailure


class RunnerAPIError(RuntimeError):
    pass


class RunnerAPIClient:
    """Small HTTP client; writes are never silently retried after ambiguity."""

    def __init__(self, server_url: str, token: str, *, timeout: float = 60):
        parsed = urllib.parse.urlsplit(server_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Runner server URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Runner server URL cannot contain credentials or a query")
        if not token:
            raise ValueError("Runner API token is required")
        self.server_url = server_url.rstrip("/")
        self._token = token
        self.timeout = timeout

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

    def json_sync(self, method: str, path: str, body=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(
                "Runner API paths must be relative to the configured server"
            )
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
            try:
                response = urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as rejected:
                if rejected.code != 401:
                    raise
                from simstack.runner_api.credentials import refreshed_token
                replacement = refreshed_token(self.server_url, request.get_header("Authorization")[7:])
                if replacement is None:
                    raise
                # Authorization rejected this request before dispatching any
                # operation, so repeating it with the new token is safe.
                request.add_header("Authorization", "Bearer " + replacement)
                response = urllib.request.urlopen(request, timeout=self.timeout)
            with response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get(
                    "detail", "Runner API request failed"
                )
            except (ValueError, AttributeError):
                detail = "Runner API request failed"
            raise RunnerAPIError(
                f"Runner API returned HTTP {exc.code}: {detail}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RunnerAPIError(
                "Runner API is unavailable; write outcome may be indeterminate"
            ) from exc

    async def json(self, method: str, path: str, body=None):
        return await asyncio.to_thread(self.json_sync, method, path, body)

    async def persistence(self, collection: str, operation: str, args, kwargs):
        request_id = str(uuid4())
        body = {
            "collection": collection,
            "operation": operation,
            "arguments": json_util.dumps({"args": args, "kwargs": kwargs}),
            "request_id": request_id,
            "repository_revision": os.environ.get("SIMSTACK_REPOSITORY_REVISION")
            or None,
        }
        response = await self.json("POST", "/runner/persistence", body)
        error = response.get("error")
        if error:
            error_type = error.get("type")
            message = error.get("message", "Persistence request failed")
            if error_type == "DuplicateKeyError":
                raise DuplicateKeyError(message, code=error.get("code", 11000))
            raise OperationFailure(message, code=error.get("code"))
        return json_util.loads(response["result"])


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
