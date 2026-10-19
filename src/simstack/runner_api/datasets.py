"""Bounded claim-scoped dataset membership reads and streaming snapshot writes."""

from __future__ import annotations

import json
import http.client
import time
from pathlib import Path

from simstack.runner_protocol.paths import dataset_section_path
from simstack.runner_api.errors import RunnerApiError

MAX_DATASET_PAGE_BYTES = 4 * 1024 * 1024


class _PrefixedBody:
    """File body with a bounded in-memory prefix, without copying its file."""

    def __init__(self, prefix, source):
        self.prefix, self.source, self.position = prefix, source, 0

    def read(self, size):
        if type(size) is not int or size < 1:
            raise ValueError("Dataset upload reads require a positive byte limit")
        stop = min(len(self.prefix), self.position + size)
        result = self.prefix[self.position:stop]
        self.position = stop
        return result + self.source.read(size - len(result))


class DatasetRunnerClient:
    def __init__(self, runner_api):
        self.api = runner_api

    @classmethod
    def from_context(cls):
        from simstack.core.context import context
        from simstack.runner_api.execution import ExecutionRunnerClient

        if (
            not context.initialized
            or not context.runner_mode
            or not isinstance(context.runner_api, ExecutionRunnerClient)
        ):
            raise RunnerApiError(
                "Dataset access requires the active execution credential"
            )
        return cls(context.runner_api)

    def _path(self, dataset_id, section_key, action, kind, **query):
        from simstack.core.runner_execution.state import current_runner_claim

        claim = current_runner_claim()
        return dataset_section_path(
            claim.task_id,
            str(dataset_id),
            section_key,
            action=action,
            kind=kind,
            claim_id=claim.claim_id,
            attempt=claim.attempt,
            **query,
        )

    def _request(self, method, path, source: Path | None = None, *, prefix: bytes = b""):
        attempts = getattr(self.api, "retry_attempts", 0) + 1
        for attempt in range(attempts):
            try:
                if prefix:
                    return self._request_once(method, path, source, prefix=prefix)
                return self._request_once(method, path, source)
            except (OSError, http.client.HTTPException, RunnerApiError) as exc:
                retryable = not isinstance(exc, RunnerApiError) or (
                    exc.status_code is not None
                    and (exc.status_code >= 500 or exc.status_code == 423)
                )
                if not retryable or attempt + 1 >= attempts:
                    raise
                time.sleep(
                    getattr(self.api, "retry_backoff_seconds", 0) * (attempt + 1)
                )
        raise AssertionError("unreachable")

    def _request_once(self, method, path, source: Path | None = None, *, prefix: bytes = b""):
        transport = self.api._transport
        connection = transport.connection()
        try:
            headers = {
                "Authorization": f"Bearer {self.api.runner_token}",
                "Accept": "application/json",
            }
            if source is None:
                connection.request(method, transport.path(path), headers=headers)
            else:
                headers.update(
                    {
                        "Content-Type": "application/x-ndjson",
                        "Content-Length": str(len(prefix) + source.stat().st_size),
                    }
                )
                with source.open("rb") as body:
                    connection.request(
                        method, transport.path(path),
                        body=_PrefixedBody(prefix, body) if prefix else body, headers=headers
                    )
            response = connection.getresponse()
            raw = response.read(MAX_DATASET_PAGE_BYTES + 1)
            if len(raw) > MAX_DATASET_PAGE_BYTES:
                raise RunnerApiError("Dataset response exceeds its page budget")
            if response.status >= 400:
                raise RunnerApiError(
                    f"Dataset API returned HTTP {response.status}: {raw.decode(errors='replace')}",
                    status_code=response.status,
                )
            try:
                parsed = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RunnerApiError("Dataset API returned invalid JSON") from exc
            if not isinstance(parsed, dict):
                raise RunnerApiError("Dataset API returned a non-object response")
            return parsed
        finally:
            connection.close()

    def page(self, storage, *, kind, section_key, offset, limit):
        return self._request(
            "GET",
            self._path(
                storage.dataset_id,
                section_key,
                "rows",
                kind,
                revision=storage.revision,
                offset=offset,
                limit=limit,
            ),
        )

    def snapshot(self, dataset_id, section_key, *, kind, source, header=None):
        prefix = b"" if header is None else json.dumps(
            header, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode() + b"\n"
        if len(prefix) > MAX_DATASET_PAGE_BYTES:
            raise ValueError("Dataset snapshot header exceeds its byte budget")
        return self._request(
            "PUT", self._path(dataset_id, section_key, "snapshot", kind), source,
            prefix=prefix,
        )

    def hydrate_documents(self, entries):
        from simstack.core.runner_execution.errors import RunnerTaskPayloadError
        from simstack.core.runner_execution.hydration import _hydrate_entries
        from simstack.models.document import ObjectId

        roots = []
        for index, entry in enumerate(entries):
            value = entry.get("value") if isinstance(entry, dict) else None
            if not isinstance(value, dict) or len({"id", "_id"} & set(value)) != 1:
                raise RunnerTaskPayloadError("Dataset row document requires exactly one canonical identity")
            identifier = value.get("id", value.get("_id"))
            try:
                canonical = str(ObjectId(identifier)) if isinstance(identifier, str) else None
            except ValueError:
                canonical = None
            if canonical is None or identifier != canonical:
                raise RunnerTaskPayloadError("Dataset row document identity must be a canonical ObjectId string")
            if entry.get("reference", identifier) != identifier:
                raise RunnerTaskPayloadError("Dataset row document reference disagrees with its identity")
            document = {name: item for name, item in value.items() if name != "_id"}
            document["id"] = identifier
            roots.append({**entry, "variable_name": str(index), "reference": identifier, "value": document})
        return {
            str(model.id): model
            for _, model in _hydrate_entries(roots, kind="dataset rows")
        }
