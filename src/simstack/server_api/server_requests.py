"""Authenticated Server requests, protocol version and request replay identity."""

from __future__ import annotations
import asyncio
from copy import deepcopy
import math
import os
from urllib.parse import urlencode
from uuid import UUID, uuid4
from simstack.server_api.http_transport import RunnerAPIClient, RunnerAPIError
from simstack.runner_runtime.core_build import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION", "ServerRequests"]


class ServerRequests:
    def __init__(self, transport, *, authorization="runner", resource="self", command_recovery_timeout=300.0):
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
        if not math.isfinite(command_recovery_timeout) or command_recovery_timeout <= 0:
            raise ValueError("Command recovery timeout must be positive and finite")
        self.command_recovery_timeout = command_recovery_timeout
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

    async def command(self, path, body, *, request_id=None, recover=False):
        """Recover opted-in receipt-backed writes under one immutable identity."""
        request_id = (
            str(UUID(str(request_id))) if request_id is not None else str(uuid4())
        )
        endpoint = self.path(path)
        payload = deepcopy({**body, "request_id": request_id}) if recover else {**body, "request_id": request_id}

        async def send():
            return await self.transport.json(
                "POST", endpoint, deepcopy(payload) if recover else payload
            )

        try:
            return await send()
        except RunnerAPIError as exc:
            if not recover or exc.status_code is not None and exc.status_code < 500:
                raise

        # Three hundred seconds permits recovery across the Server's two-minute
        # stale request lease without extending individual HTTP timeouts.
        delay = 0.1
        try:
            async with asyncio.timeout(self.command_recovery_timeout):
                while True:
                    try:
                        receipt = await self.read(f"/requests/{request_id}")
                    except RunnerAPIError as exc:
                        if exc.status_code is not None and exc.status_code < 500:
                            raise
                    else:
                        if receipt["state"] == "applied":
                            return receipt["result"]
                        if receipt["state"] == "rejected":
                            raise RunnerAPIError(
                                str(receipt.get("detail") or "Command was rejected"),
                                status_code=receipt.get("status_code", 409),
                            )
                        if receipt["state"] not in {"pending", "not_found"}:
                            raise RunnerAPIError(f"Unknown command receipt state for {endpoint}: {request_id}")
                        try:
                            return await send()
                        except RunnerAPIError as exc:
                            if exc.status_code is not None and exc.status_code < 500:
                                raise
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 1.0)
        except TimeoutError as exc:
            raise RunnerAPIError(
                f"Runner API write outcome remains indeterminate after recovery: {endpoint}; request_id={request_id}"
            ) from exc

    async def read(self, path, **params):
        return await self.transport.json("GET", self.path(path, params))

    async def query(self, path, body):
        return await self.transport.json(
            "POST", self.path(path), body, safe_to_retry=True
        )
