"""Authenticated Server requests, protocol version and request replay identity."""

from __future__ import annotations
import os
from urllib.parse import urlencode
from uuid import UUID, uuid4
from simstack.server_api.http_transport import RunnerAPIClient
from simstack.runner_runtime.core_build import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION", "ServerRequests"]


class ServerRequests:
    def __init__(self, transport, *, authorization="runner", resource="self"):
        if authorization not in {"runner", "user", "local"}:
            raise ValueError("Unknown Server authorization role")
        if (
            authorization in {"user", "local"}
            and isinstance(transport, RunnerAPIClient)
            and transport.rotate_credentials
        ):
            raise ValueError(
                "User SDK transport must disable Runner credential rotation"
            )
        self.transport = transport
        self.authorization = authorization
        self.resource = resource
        self.prefix = {"runner": "/runner/v3", "user": "/user-runtime/v3",
                       "local": "/local-execution/v3"}[authorization]
        self.object_api_prefix = {"runner": "/runner/objects", "user": "/files/objects",
                                  "local": "/local-execution/v3/objects"}[authorization]
        self.bootstrap_data = None

    @property
    def server_url(self):
        return self.transport.server_url

    @property
    def token(self):
        return self.transport.token

    def revision(self):
        return os.environ.get("SIMSTACK_REPOSITORY_REVISION")

    def path(self, path, params=None):
        query = {
            key: value for key, value in (params or {}).items() if value is not None
        }
        if self.authorization == "user":
            query.setdefault("resource", self.resource)
        result = self.prefix + path
        return result + "?" + urlencode(query) if query else result

    async def command(self, path, body, *, request_id=None):
        """Send a command once under its caller's stable replay identity."""
        request_id = (
            str(UUID(str(request_id))) if request_id is not None else str(uuid4())
        )
        return await self.transport.json(
            "POST", self.path(path), {**body, "request_id": request_id}
        )

    async def read(self, path, **params):
        return await self.transport.json("GET", self.path(path, params))

    async def query(self, path, body):
        return await self.transport.json(
            "POST", self.path(path), body, safe_to_retry=True
        )
