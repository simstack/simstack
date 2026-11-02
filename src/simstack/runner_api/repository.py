"""Persistence-neutral document adapters shared by API clients and Server.

Models own validation. This adapter owns reference graphs and partial writes;
the Server backend alone translates these operations into database commands.
"""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, get_args, get_origin
from uuid import uuid4

from simstack.models.document import EmbeddedModel, Model

SNAPSHOT = "_simstack_persisted_document"


class DocumentNotFoundError(LookupError):
    """A requested document no longer exists."""


def collection_name(model):
    return model if isinstance(model, str) else model.__collection__


def model_mapping(model):
    return f"{model.__module__}.{model.__qualname__}"


def wire(value):
    from simstack.models.document.codec import encode_document_value

    return encode_document_value(value)


def unwire(value):
    from simstack.models.document.codec import decode_document_value

    return decode_document_value(value)


def criteria_tree(criteria):
    """Accept neutral class-field criteria; never accept database operators."""
    from simstack.persistence.criteria import normalize_criteria

    return wire(normalize_criteria(*criteria))


def sort_values(sort):
    if sort is None:
        return []
    values = sort if isinstance(sort, (list, tuple)) else [sort]
    result = []
    for value in values:
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        elif hasattr(value, "path"):
            value = {"field": value.path, "direction": 1}
        elif isinstance(value, str):
            value = {"field": value, "direction": 1}
        result.append(value)
    return result


def _model_type(annotation):
    if isinstance(annotation, type) and issubclass(annotation, (Model, EmbeddedModel)):
        return annotation
    for arg in get_args(annotation):
        found = _model_type(arg)
        if found:
            return found
    return None


class DocumentRepository:
    """Typed adapter, independent of MongoDB and HTTP."""

    async def query_documents(
        self, model, filter, sort, offset, limit, *, model_mapping=None
    ):
        raise NotImplementedError

    async def save_documents(self, model, documents):
        raise NotImplementedError

    async def delete_document(self, model, id, *, model_mapping=None):
        raise NotImplementedError

    async def task_mutation(self, task_id, action, values=None, expected_statuses=None):
        raise NotImplementedError

    async def find(self, model, *criteria, sort=None, skip=0, limit=None, **kwargs):
        if not isinstance(model, type) or not issubclass(model, Model):
            raise TypeError("find requires a document Model class")
        if kwargs:
            raise TypeError("Unsupported query options: " + ", ".join(kwargs))
        rows = await self.query_documents(
            collection_name(model),
            criteria_tree(criteria),
            sort_values(sort),
            skip,
            limit,
            model_mapping=model_mapping(model),
        )
        return [await self.hydrate(model, row) for row in rows]

    async def find_one(self, model, *criteria, **kwargs):
        kwargs["limit"] = 1
        rows = await self.find(model, *criteria, **kwargs)
        return rows[0] if rows else None

    async def hydrate(self, model, document, seen=None):
        from simstack.models.document import SUBTYPE_KEY, resolve_document_type

        value = unwire(deepcopy(document))
        model = resolve_document_type(model, value)
        seen = {} if seen is None else seen
        identity = (collection_name(model), str(value.get("_id", value.get("id"))))
        if identity in seen:
            raise ValueError("Cyclic persisted reference graph")
        seen = {**seen, identity: True}

        async def expand(annotation, item, reference=False):
            if item is None:
                return None
            if isinstance(item, list):
                args = get_args(annotation)
                return [
                    await expand(
                        args[index]
                        if get_origin(annotation) is tuple
                        and len(args) > 1
                        and args[-1] is not Ellipsis
                        else args[0]
                        if args
                        else annotation,
                        child,
                        reference,
                    )
                    for index, child in enumerate(item)
                ]
            if isinstance(item, dict) and get_origin(annotation) is dict:
                args = get_args(annotation)
                child_type = args[1] if len(args) > 1 else Any
                return {
                    key: await expand(child_type, child, reference)
                    for key, child in item.items()
                }
            target = _model_type(annotation)
            if target is None:
                return item
            target = resolve_document_type(target, item)
            if (
                reference
                and issubclass(target, Model)
                and (
                    not isinstance(item, dict)
                    or set(item) <= {"id", "_id", SUBTYPE_KEY}
                )
            ):
                item_id = (
                    item.get("id", item.get("_id")) if isinstance(item, dict) else item
                )
                rows = await self.query_documents(
                    collection_name(target),
                    {"op": "eq", "field": "id", "value": wire(item_id)},
                    [],
                    0,
                    1,
                    model_mapping=model_mapping(target),
                )
                if not rows:
                    raise ValueError(f"Missing reference {target.__name__}:{item}")
                return await self.hydrate(target, rows[0], seen)
            if isinstance(item, dict):
                return target.model_validate(
                    await self._expand_embedded(target, item, expand)
                )
            return item

        value = await self._expand_embedded(model, value, expand)
        if "_id" in value and "id" not in value:
            value["id"] = value.pop("_id")
        value.pop(SUBTYPE_KEY, None)
        instance = model.model_validate(value)
        self.remember(instance)
        return instance

    @staticmethod
    async def _expand_embedded(model, value, expand):
        from simstack.models.document import SUBTYPE_KEY, resolve_document_type
        from simstack.models.document.fields import REFERENCE_SCHEMA_KEY

        model = resolve_document_type(model, value)
        result = dict(value)
        for name, field in model.model_fields.items():
            key = field.alias if field.alias in result else name
            if key in result:
                result[key] = await expand(
                    field.annotation,
                    result[key],
                    bool((field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY)),
                )
        result.pop(SUBTYPE_KEY, None)
        return result

    @staticmethod
    def remember(instance):
        object.__setattr__(instance, SNAPSHOT, deepcopy(instance.model_dump_doc()))

    @staticmethod
    def _validate_persisted_identities(value, seen=None):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, Model):
            snapshot = getattr(value, SNAPSHOT, None)
            if snapshot is not None and snapshot.get(
                "id"
            ) != value.model_dump_doc().get("id"):
                raise ValueError(
                    "The identity of a persisted document cannot be changed"
                )
        if isinstance(value, (Model, EmbeddedModel)):
            children = [getattr(value, name) for name in type(value).model_fields]
        elif isinstance(value, dict):
            children = value.values()
        elif isinstance(value, (list, tuple, set)):
            children = value
        else:
            return
        for child in children:
            DocumentRepository._validate_persisted_identities(child, seen)

    async def save(self, instance, *, _seen=None, **kwargs):
        if kwargs:
            raise TypeError("Explicit database sessions are not supported")
        if not isinstance(instance, Model):
            raise TypeError("save requires a document Model instance")
        # Validate before the first write: changing a saved key must not insert
        # another record or leave previously visited references partly saved.
        if _seen is None:
            self._validate_persisted_identities(instance)
        from simstack.models.document.fields import REFERENCE_SCHEMA_KEY

        seen = set() if _seen is None else _seen
        if id(instance) in seen:
            return instance
        seen.add(id(instance))

        async def save_references(value, reference=False):
            if isinstance(value, Model) and reference:
                await self.save(value, _seen=seen)
            elif isinstance(value, (Model, EmbeddedModel)):
                for name, field in type(value).model_fields.items():
                    await save_references(
                        getattr(value, name),
                        bool((field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY)),
                    )
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    await save_references(item, reference)
            elif isinstance(value, dict):
                for item in value.values():
                    await save_references(item, reference)

        for name, field in type(instance).model_fields.items():
            await save_references(
                getattr(instance, name),
                bool((field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY)),
            )
        document = instance.model_dump_doc()
        snapshot = getattr(instance, SNAPSHOT, None)
        changed = [
            name
            for name, value in document.items()
            if name not in {"id", "_id"}
            and (snapshot is None or snapshot.get(name) != value)
        ]
        ignored = getattr(instance, "_simstack_refreshed_fields", set())
        changed = [name for name in changed if name not in ignored]
        await self.save_documents(
            collection_name(type(instance)),
            [
                {
                    "document": document,
                    "changed_fields": changed,
                    "model_mapping": model_mapping(type(instance)),
                }
            ],
        )
        self.remember(instance)
        object.__setattr__(instance, "_simstack_refreshed_fields", set())
        return instance

    async def delete(self, instance, **kwargs):
        if kwargs:
            raise TypeError("Explicit database sessions are not supported")
        deleted = await self.delete_document(
            collection_name(type(instance)),
            instance.id,
            model_mapping=model_mapping(type(instance)),
        )
        if deleted is False:
            raise DocumentNotFoundError(
                f"{type(instance).__name__}:{instance.id} does not exist"
            )


class HTTPRepository(DocumentRepository):
    def __init__(self, api):
        self.api = api

    @staticmethod
    def revision():
        return os.environ.get("SIMSTACK_REPOSITORY_REVISION") or None

    async def query_documents(
        self, model, filter, sort, offset, limit, *, model_mapping=None
    ):
        response = await self.api.json(
            "POST",
            "/runner/data/query",
            {
                "model": model,
                "filter": filter,
                "sort": sort,
                "offset": offset,
                "limit": limit,
                "model_mapping": model_mapping,
                "repository_revision": self.revision(),
            },
        )
        return response["documents"]

    async def save_documents(self, model, documents):
        response = await self.api.json(
            "POST",
            "/runner/data/save",
            {
                "model": model,
                "documents": documents,
                "repository_revision": self.revision(),
                "request_id": str(uuid4()),
            },
        )
        return response["documents"]

    async def delete_document(self, model, id, *, model_mapping=None):
        response = await self.api.json(
            "POST",
            "/runner/data/delete",
            {
                "model": model,
                "id": wire(id),
                "model_mapping": model_mapping,
                "request_id": str(uuid4()),
                "repository_revision": self.revision(),
            },
        )
        return response["deleted"]

    async def task_mutation(self, task_id, action, values=None, expected_statuses=None):
        response = await self.api.json(
            "POST",
            f"/runner/tasks/{task_id}/"
            + ("claim" if action == "claim" else "mutation"),
            {
                "action": action,
                "values": wire(values or {}),
                "expected_statuses": expected_statuses,
                "request_id": str(uuid4()),
                "repository_revision": self.revision(),
            },
        )
        return response.get("document")
