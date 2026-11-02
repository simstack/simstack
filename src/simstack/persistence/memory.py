"""Isolated document storage for tests and catalogue inspection."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from simstack.runner_api.repository import (
    DocumentRepository,
    collection_name,
    unwire,
    wire,
)


def field_value(document, path):
    values = path.split(".")
    current = document
    for field in values:
        if field in {"id", "_id"}:
            field = "_id" if "_id" in current else "id"
        if not isinstance(current, dict):
            return None
        current = current.get(field)
    return current


def matches(document, criteria):
    op = criteria.get("op", "all")
    if op in {"all", "any", "and", "or", "not"}:
        found = [
            matches(document, item)
            for item in criteria.get("criteria", criteria.get("items", []))
        ]
        return (
            all(found)
            if op in {"all", "and"}
            else any(found)
            if op in {"any", "or"}
            else not all(found)
        )
    value = field_value(document, criteria["field"])
    expected = criteria.get("value")
    if op == "eq":
        return (
            expected in value
            if isinstance(value, list) and not isinstance(expected, list)
            else value == expected
        )
    if op == "ne":
        return not matches(document, {**criteria, "op": "eq"})
    if op == "in":
        return (
            any(item in expected for item in value)
            if isinstance(value, list)
            else value in expected
        )
    if op in {"nin", "not_in"}:
        return not matches(document, {**criteria, "op": "in"})
    if op == "exists":
        return (value is not None) == bool(expected)
    if value is None:
        return False
    if op == "gt":
        return value > expected
    if op == "gte":
        return value >= expected
    if op == "lt":
        return value < expected
    if op == "lte":
        return value <= expected
    if op == "regex":
        import re

        return re.search(expected, str(value)) is not None
    raise ValueError(f"Unsupported criterion {op}")


class InMemoryRepository(DocumentRepository):
    def __init__(self):
        self._store = {}
        self._lock = asyncio.Lock()

    async def query_documents(
        self, model, filter, sort, offset, limit, *, model_mapping=None
    ):
        result = [
            deepcopy(value)
            for value in self._store.get(model, {}).values()
            if matches(unwire(value), unwire(filter))
        ]
        for item in reversed(sort):
            result.sort(
                key=lambda row: (
                    field_value(unwire(row), item["field"]) is not None,
                    field_value(unwire(row), item["field"]),
                ),
                reverse=item["direction"] in {-1, "desc"},
            )
        return result[offset:] if limit is None else result[offset : offset + limit]

    async def save_documents(self, model, documents):
        async with self._lock:
            collection = self._store.setdefault(model, {})
            result = []
            for change in documents:
                incoming = deepcopy(change["document"])
                key = str(incoming.get("_id", incoming.get("id")))
                existing = collection.get(key)
                if existing is None:
                    existing = incoming
                else:
                    for field in change["changed_fields"]:
                        if field in incoming:
                            existing[field] = incoming[field]
                collection[key] = existing
                result.append(deepcopy(existing))
            return result

    async def delete_document(self, model, id, *, model_mapping=None):
        return self._store.get(model, {}).pop(str(id), None) is not None

    async def task_mutation(self, task_id, action, values=None, expected_statuses=None):
        async with self._lock:
            entry = self._store.get("node_registry", {}).get(str(task_id))
            if entry is None:
                return None
            if action == "claim":
                expected_statuses = ["submitted"]
                values = {"status": "retrieved"}
            if (
                expected_statuses is not None
                and entry.get("status") not in expected_statuses
            ):
                return None
            values = wire(values or {})
            if action == "link_parent":
                parents = entry.setdefault("parent_ids", [])
                if values["parent_id"] not in parents:
                    parents.append(values["parent_id"])
            else:
                for path, value in values.items():
                    current = entry
                    fields = path.split(".")
                    for field in fields[:-1]:
                        current = current.setdefault(field, {})
                    current[fields[-1]] = value
            return deepcopy(entry)

    async def reset(self):
        self._store.clear()

    async def ping(self):
        return True

    async def stats(self):
        return {
            "collections": len(self._store),
            "objects": sum(len(rows) for rows in self._store.values()),
        }

    def get_collection(self, model):
        """Local fixture inspection only; this interface is never exposed by HTTP."""
        return MemoryCollection(self, collection_name(model))

    @property
    def database(self):
        return self

    def __getitem__(self, name):
        return self.get_collection(name)

    async def list_collection_names(self):
        return list(self._store)


def _local_filter(value):
    items = []
    for field, expected in (value or {}).items():
        if field in {"$and", "$or"}:
            items.append(
                {"op": field[1:], "items": [_local_filter(item) for item in expected]}
            )
        elif isinstance(expected, dict) and any(
            str(key).startswith("$") for key in expected
        ):
            for operator, operand in expected.items():
                items.append(
                    {"op": operator.lstrip("$"), "field": field, "value": operand}
                )
        else:
            items.append({"op": "eq", "field": field, "value": expected})
    return {"op": "and", "items": items}


class MemoryCursor:
    def __init__(self, rows):
        self.rows = rows
        self.index = 0

    async def to_list(self, length=None):
        rows = (
            self.rows[self.index :]
            if length is None
            else self.rows[self.index : self.index + length]
        )
        self.index += len(rows)
        return deepcopy(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        rows = await self.to_list(1)
        if not rows:
            raise StopAsyncIteration
        return rows[0]


class MemoryCollection:
    def __init__(self, repository, name):
        self.repository, self.name = repository, name

    def find(self, filter=None):
        return MemoryCursor(
            [
                unwire(deepcopy(row))
                for row in self.repository._store.get(self.name, {}).values()
                if matches(unwire(row), _local_filter(filter))
            ]
        )

    async def find_one(self, filter=None, *args, **kwargs):
        rows = await self.find(filter).to_list(1)
        return rows[0] if rows else None

    async def insert_one(self, row):
        from types import SimpleNamespace

        from simstack.models.document import ObjectId

        row = wire(deepcopy(row))
        identity = str(row.get("id", row.get("_id")) or ObjectId())
        row.setdefault("id", identity)
        self.repository._store.setdefault(self.name, {})[identity] = row
        return SimpleNamespace(inserted_id=identity)

    async def insert_many(self, rows):
        from types import SimpleNamespace

        return SimpleNamespace(
            inserted_ids=[(await self.insert_one(row)).inserted_id for row in rows]
        )

    async def delete_many(self, filter):
        from types import SimpleNamespace

        collection = self.repository._store.get(self.name, {})
        keys = [
            key
            for key, row in collection.items()
            if matches(unwire(row), _local_filter(filter))
        ]
        for key in keys:
            del collection[key]
        return SimpleNamespace(deleted_count=len(keys))

    async def delete_one(self, filter):
        from types import SimpleNamespace

        collection = self.repository._store.get(self.name, {})
        key = next(
            (
                key
                for key, row in collection.items()
                if matches(unwire(row), _local_filter(filter))
            ),
            None,
        )
        if key is not None:
            del collection[key]
        return SimpleNamespace(deleted_count=int(key is not None))

    async def replace_one(self, filter, row, upsert=False):
        from types import SimpleNamespace

        existing = await self.find_one(filter)
        if existing is None and not upsert:
            return SimpleNamespace(matched_count=0, modified_count=0)
        row = dict(row)
        if existing:
            row.setdefault("id", existing.get("id", existing.get("_id")))
        await self.insert_one(row)
        return SimpleNamespace(
            matched_count=int(existing is not None), modified_count=1
        )

    async def count_documents(self, filter):
        return len(await self.find(filter).to_list())

    async def drop(self):
        self.repository._store.pop(self.name, None)
