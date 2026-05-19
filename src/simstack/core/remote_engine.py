"""AIOEngine-compatible client that delegates database operations to simstack-server."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Type, TypeVar

import httpx
from odmantic import Model

from simstack.util.db_serialization import (
    deserialize_model,
    model_fqn,
    serialize_find_kwargs,
    serialize_model,
    serialize_models,
    serialize_query,
)
from simstack.util.importer import import_class

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Model)


class RemoteMotorCollection:
    """Subset of Motor collection operations used by simstack via get_collection()."""

    def __init__(self, engine: "RemoteAIOEngine", model_class: Type[Model]):
        self._engine = engine
        self._model_class = model_class

    async def find_one(self, query: Dict[str, Any], *args, **kwargs) -> Optional[Dict[str, Any]]:
        return await self._engine._request(
            "POST",
            "/database/collection/find-one",
            {
                "model": model_fqn(self._model_class),
                "query": serialize_query(query),
            },
        )

    async def find(self, query: Dict[str, Any], *args, **kwargs):
        return _RemoteCursor(
            await self._engine._request(
                "POST",
                "/database/collection/find",
                {
                    "model": model_fqn(self._model_class),
                    "query": serialize_query(query) or {},
                },
            )
        )

    async def insert_one(self, document: Dict[str, Any], *args, **kwargs):
        result = await self._engine._request(
            "POST",
            "/database/collection/insert-one",
            {
                "model": model_fqn(self._model_class),
                "document": document,
            },
        )
        return _InsertOneResult(result.get("inserted_id"))

    async def replace_one(self, query: Dict[str, Any], document: Dict[str, Any], *args, **kwargs):
        await self._engine._request(
            "POST",
            "/database/collection/replace-one",
            {
                "model": model_fqn(self._model_class),
                "query": serialize_query(query),
                "document": document,
            },
        )

    async def aggregate(self, pipeline: List[Dict[str, Any]], *args, **kwargs):
        return _RemoteCursor(
            await self._engine._request(
                "POST",
                "/database/aggregate",
                {
                    "model": model_fqn(self._model_class),
                    "pipeline": pipeline,
                },
            )
        )

    async def drop(self) -> None:
        await self._engine._request(
            "POST",
            "/database/drop-collection",
            {"model": model_fqn(self._model_class)},
        )


class _RemoteCursor:
    def __init__(self, documents: List[Dict[str, Any]]):
        self._documents = documents

    async def to_list(self, length: Optional[int] = None) -> List[Dict[str, Any]]:
        if length is None:
            return list(self._documents)
        return list(self._documents[:length])


class _InsertOneResult:
    def __init__(self, inserted_id: Any):
        from odmantic import ObjectId

        if isinstance(inserted_id, str):
            try:
                inserted_id = ObjectId(inserted_id)
            except Exception:
                pass
        self.inserted_id = inserted_id


class RemoteAIOEngine:
    """
    Engine proxy that forwards ODMantic-style operations to simstack-server database routes.
  Uses the authenticated user's database on the server.
    """

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.database_name = "remote"
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._client

    async def _request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Any:
        client = await self._get_client()
        response = await client.request(method, path, json=json_body)
        if response.status_code >= 400:
            detail = response.text
            try:
                payload = response.json()
                detail = payload.get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"Remote database request failed ({response.status_code}): {detail}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> "RemoteAIOEngine":
        """Compatibility with code that accesses engine.client (e.g. user_engines)."""
        return self

    def get_collection(self, model_class: Type[Model]) -> RemoteMotorCollection:
        return RemoteMotorCollection(self, model_class)

    async def save(self, obj: Any, *args, **kwargs) -> Any:
        if isinstance(obj, (list, tuple, set)):
            return [await self._save_one(item, *args, **kwargs) for item in obj]
        return await self._save_one(obj, *args, **kwargs)

    async def save_unchecked(self, obj: Any, *args, **kwargs) -> Any:
        return await self._save_one(obj, unchecked=True, *args, **kwargs)

    async def save_all(self, instances: Iterable[Any], *args, **kwargs) -> list[Any]:
        models = list(instances)
        if not models:
            return []
        payload = await self._request(
            "POST",
            "/database/save-all",
            {
                "models": [
                    {
                        "model": model_fqn(type(instance)),
                        "document": serialize_model(instance),
                    }
                    for instance in models
                ],
                "unchecked": False,
            },
        )
        return [
            deserialize_model(await import_class(item["model"]), item["document"])
            for item in payload["results"]
        ]

    async def _save_one(self, model: Any, *, unchecked: bool = False, *args, **kwargs) -> Any:
        path = "/database/save-unchecked" if unchecked else "/database/save"
        payload = await self._request(
            "POST",
            path,
            {
                "model": model_fqn(type(model)),
                "document": serialize_model(model),
            },
        )
        model_class = await import_class(payload["model"])
        return deserialize_model(model_class, payload["document"])

    async def find_one(
        self,
        model_class: Type[T],
        *queries,
        **kwargs,
    ) -> Optional[T]:
        serialized_queries = [serialize_query(q) for q in queries if q is not None]
        payload = await self._request(
            "POST",
            "/database/find-one",
            {
                "model": model_fqn(model_class),
                "queries": serialized_queries,
                "kwargs": serialize_find_kwargs(kwargs),
            },
        )
        if payload is None:
            return None
        return deserialize_model(model_class, payload["document"])

    async def find(self, model_class: Type[T], *queries, **kwargs) -> List[T]:
        serialized_queries = [serialize_query(q) for q in queries if q is not None]
        payload = await self._request(
            "POST",
            "/database/find",
            {
                "model": model_fqn(model_class),
                "queries": serialized_queries,
                "kwargs": serialize_find_kwargs(kwargs),
            },
        )
        documents = payload.get("documents", [])
        return [deserialize_model(model_class, doc) for doc in documents]

    async def delete(self, model: Model) -> None:
        await self._request(
            "POST",
            "/database/delete",
            {
                "model": model_fqn(type(model)),
                "document": serialize_model(model),
            },
        )

    async def count(self, model_class: Type[T], *queries, **kwargs) -> int:
        serialized_queries = [serialize_query(q) for q in queries if q is not None]
        payload = await self._request(
            "POST",
            "/database/count",
            {
                "model": model_fqn(model_class),
                "queries": serialized_queries,
                "kwargs": serialize_find_kwargs(kwargs),
            },
        )
        return int(payload["count"])

    async def list_collection_names(self) -> List[str]:
        payload = await self._request("GET", "/database/collections")
        return list(payload.get("collections", []))

    async def reset_database(self) -> None:
        await self._request("POST", "/database/reset")
