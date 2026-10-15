"""Explicit in-memory implementation of the SimStack repository port for tests."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from simstack.models.document import EmbeddedModel, Model, ObjectId, decode_document_value
from simstack.persistence import (
    Comparison,
    Criteria,
    CriteriaGroup,
    FieldCriterion,
    Junction,
    Sort,
    SortDirection,
    eq,
    in_values,
)


def _nested_values(value: Any, path: tuple[str, ...]) -> list[Any]:
    if not path:
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [
            nested
            for item in value
            for nested in _nested_values(item, path)
        ]
    head, *tail = path
    if isinstance(value, dict):
        next_value = value.get(head)
    else:
        next_value = getattr(value, head, None)
    return _nested_values(next_value, tuple(tail))


def _matches_field(value: Model, criterion: FieldCriterion) -> bool:
    actual_values = _nested_values(value, tuple(criterion.field.split(".")))
    expected = criterion.value
    comparison = criterion.comparison

    def compare(actual: Any) -> bool:
        if comparison is Comparison.EQUAL:
            return expected in actual if isinstance(actual, list) else actual == expected
        if comparison is Comparison.NOT_EQUAL:
            return expected not in actual if isinstance(actual, list) else actual != expected
        if comparison is Comparison.LESS_THAN:
            return actual is not None and actual < expected
        if comparison is Comparison.LESS_THAN_OR_EQUAL:
            return actual is not None and actual <= expected
        if comparison is Comparison.GREATER_THAN:
            return actual is not None and actual > expected
        if comparison is Comparison.GREATER_THAN_OR_EQUAL:
            return actual is not None and actual >= expected
        if comparison is Comparison.IN:
            candidates = set(expected)
            if isinstance(actual, (list, tuple, set)):
                return any(item in candidates for item in actual)
            return actual in candidates
        if comparison is Comparison.NOT_IN:
            candidates = set(expected)
            if isinstance(actual, (list, tuple, set)):
                return all(item not in candidates for item in actual)
            return actual not in candidates
        raise AssertionError(f"Unhandled comparison: {comparison}")

    return any(compare(actual) for actual in actual_values)


def _matches(value: Model, criterion: Criteria) -> bool:
    if isinstance(criterion, FieldCriterion):
        return _matches_field(value, criterion)
    if not isinstance(criterion, CriteriaGroup):
        raise TypeError("Repository queries accept Criteria objects only")
    matches = [_matches(value, nested) for nested in criterion.criteria]
    return all(matches) if criterion.junction is Junction.ALL else any(matches)


def _criterion_from_raw(field: str, value: Any) -> Criteria:
    canonical = "id" if field == "_id" else field
    if not isinstance(value, dict):
        return eq(canonical, value)
    operators = {
        "$eq": Comparison.EQUAL,
        "$ne": Comparison.NOT_EQUAL,
        "$lt": Comparison.LESS_THAN,
        "$lte": Comparison.LESS_THAN_OR_EQUAL,
        "$gt": Comparison.GREATER_THAN,
        "$gte": Comparison.GREATER_THAN_OR_EQUAL,
        "$in": Comparison.IN,
        "$nin": Comparison.NOT_IN,
    }
    if len(value) != 1 or next(iter(value)) not in operators:
        raise ValueError(f"Unsupported test collection query for {field}: {value}")
    operator, operand = next(iter(value.items()))
    if operators[operator] is Comparison.IN:
        return in_values(canonical, operand)
    return FieldCriterion(canonical, operators[operator], operand)


def _criteria_from_raw(query: dict[str, Any]) -> tuple[Criteria, ...]:
    criteria: list[Criteria] = []
    for field, value in query.items():
        if field in {"$and", "$or"}:
            nested = tuple(
                criterion
                for item in value
                for criterion in _criteria_from_raw(item)
            )
            criteria.append(
                CriteriaGroup(
                    Junction.ALL if field == "$and" else Junction.ANY,
                    nested,
                )
            )
        else:
            criteria.append(_criterion_from_raw(field, value))
    return tuple(criteria)


@dataclass(slots=True)
class _WriteResult:
    inserted_id: ObjectId | None = None
    matched_count: int = 0
    modified_count: int = 0
    upserted_id: ObjectId | None = None


class _Cursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = documents

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        if length is None:
            return copy.deepcopy(self._documents)
        return copy.deepcopy(self._documents[:length])


class InMemoryCollection:
    def __init__(self, repository: "InMemoryRepository", model: type[Model]) -> None:
        self._repository = repository
        self._model = model
        self.name = str(model.__collection__)

    async def drop(self) -> None:
        await self._repository.delete_all(self._model)

    async def insert_one(self, document: dict[str, Any]) -> _WriteResult:
        model = self._model.model_validate(copy.deepcopy(document))
        await self._repository.save(model)
        return _WriteResult(inserted_id=model.id, matched_count=1, modified_count=1)

    async def replace_one(
        self,
        query: dict[str, Any],
        document: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> _WriteResult:
        existing = await self._find_model(query)
        if existing is None and not upsert:
            return _WriteResult()
        payload = copy.deepcopy(document)
        if existing is not None:
            payload.setdefault("_id", existing.id)
        model = self._model.model_validate(payload)
        await self._repository.save(model)
        return _WriteResult(
            matched_count=int(existing is not None),
            modified_count=1,
            upserted_id=model.id if existing is None else None,
        )

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> _WriteResult:
        existing = await self._find_model(query)
        if existing is None:
            if not upsert:
                return _WriteResult()
            base = {
                field: value
                for field, value in query.items()
                if not field.startswith("$") and not isinstance(value, dict)
            }
            base.update(update.get("$setOnInsert", {}))
            existing = self._model.model_validate(base)
        payload = existing.model_dump(mode="python", by_alias=False)
        payload.update(update.get("$set", {}))
        for field in update.get("$unset", {}):
            payload.pop(field, None)
        model = self._model.model_validate(payload)
        await self._repository.save(model)
        return _WriteResult(matched_count=1, modified_count=1)

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        result = await self.update_one(
            query,
            update,
            upsert=bool(kwargs.get("upsert", False)),
        )
        if result.modified_count == 0:
            return None
        return await self.find_one(query)

    async def find_one(
        self,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        model = await self._find_model(query or {})
        return None if model is None else decode_document_value(model.model_dump_doc())

    def find(self, query: dict[str, Any] | None = None) -> _Cursor:
        models = self._repository._matching_sync(
            self._model,
            *_criteria_from_raw(query or {}),
        )
        return _Cursor(
            [decode_document_value(model.model_dump_doc()) for model in models]
        )

    async def delete_one(self, query: dict[str, Any]) -> _WriteResult:
        existing = await self._find_model(query)
        if existing is None:
            return _WriteResult()
        await self._repository.delete(existing)
        return _WriteResult(matched_count=1, modified_count=1)

    async def delete_many(self, query: dict[str, Any]) -> _WriteResult:
        matches = self._repository._matching_sync(
            self._model,
            *_criteria_from_raw(query),
        )
        for model in matches:
            await self._repository.delete(model)
        return _WriteResult(matched_count=len(matches), modified_count=len(matches))

    async def _find_model(self, query: dict[str, Any]) -> Model | None:
        return await self._repository.find_one(
            self._model,
            *_criteria_from_raw(query),
        )


class _DatabaseView:
    def __init__(self, repository: "InMemoryRepository") -> None:
        self._repository = repository
        self.name = "in-memory"

    def __getitem__(self, collection_name: str) -> InMemoryCollection:
        return self._repository.collection_by_name(collection_name)

    async def list_collection_names(self) -> list[str]:
        return sorted(self._repository._models_by_collection)

    async def command(self, command: Any) -> dict[str, Any]:
        name = command if isinstance(command, str) else next(iter(command))
        if name == "ping":
            return {"ok": 1.0}
        if name == "dbStats":
            return {
                "db": self.name,
                "collections": len(self._repository._store),
                "objects": sum(len(values) for values in self._repository._store.values()),
            }
        raise NotImplementedError(name)


class InMemoryRepository:
    """Test adapter implementing the public repository port without a DB driver."""

    def __init__(self) -> None:
        self._store: dict[str, dict[ObjectId, Model]] = {}
        self._models_by_collection: dict[str, type[Model]] = {}
        self.raw_database = _DatabaseView(self)
        self.database = self.raw_database
        self.database_name = "in-memory"

    def _register(self, model: type[Model]) -> str:
        collection = str(model.__collection__)
        self._models_by_collection[collection] = model
        self._store.setdefault(collection, {})
        return collection

    async def save(self, instance: Model) -> Model:
        if not isinstance(instance, Model):
            raise TypeError("InMemoryRepository.save expects one Model")
        await self._before_save(instance)
        collection = self._register(type(instance))
        self._store[collection][instance.id] = copy.deepcopy(instance)
        return instance

    async def update_node_registry(self, task_id, changes, *, expected=None):
        from simstack.models.node_registry import NodeRegistry

        allowed_fields = set(NodeRegistry.model_fields) - {"id"}
        if set(changes) - allowed_fields or set(expected or {}) - allowed_fields:
            raise ValueError("Invalid NodeRegistry update fields")
        current = self._store[self._register(NodeRegistry)].get(ObjectId(str(task_id)))
        if current is None or any(
            getattr(current, field) != value for field, value in (expected or {}).items()
        ):
            return None
        payload = current.model_dump(mode="python")
        payload.update(copy.deepcopy(changes))
        updated = NodeRegistry.model_validate(payload)
        # No await between comparison and replacement: this is the test
        # adapter's equivalent of the server's one-document CAS.
        self._store[str(NodeRegistry.__collection__)][updated.id] = copy.deepcopy(updated)
        result = copy.deepcopy(updated)
        await self._after_load(result)
        return result

    async def add_node_parent(self, task_id, parent_id):
        from simstack.models.node_registry import NodeRegistry

        current = self._store[self._register(NodeRegistry)].get(ObjectId(str(task_id)))
        if current is None:
            return None
        parent_id = ObjectId(str(parent_id))
        if parent_id not in current.parent_ids:
            current.parent_ids.append(parent_id)
        return copy.deepcopy(current)

    async def find(
        self,
        model: type[Model],
        *criteria: Criteria,
        sort: Sort | tuple[Sort, ...] | list[Sort] | None = None,
        skip: int = 0,
        limit: int | None = None,
        **kwargs: Any,
    ) -> list[Model]:
        values = self._matching_sync(model, *criteria)
        sort_entries = [] if sort is None else ([sort] if isinstance(sort, Sort) else list(sort))
        for entry in reversed(sort_entries):
            values.sort(
                key=lambda item: _nested_values(
                    item, tuple(entry.field.split("."))
                )[0],
                reverse=entry.direction is SortDirection.DESCENDING,
            )
        values = values[skip:]
        if limit is not None:
            values = values[:limit]
        results = [copy.deepcopy(value) for value in values]
        for result in results:
            await self._after_load(result)
        return results

    async def find_one(
        self,
        model: type[Model],
        *criteria: Criteria,
        **kwargs: Any,
    ) -> Model | None:
        results = await self.find(model, *criteria, limit=1)
        return results[0] if results else None

    def _matching_sync(
        self,
        model: type[Model],
        *criteria: Criteria,
    ) -> list[Model]:
        if not all(
            isinstance(criterion, (FieldCriterion, CriteriaGroup))
            for criterion in criteria
        ):
            raise TypeError("Repository queries accept Criteria objects only")
        collection = self._register(model)
        return [
            value
            for value in self._store[collection].values()
            if isinstance(value, model)
            and all(_matches(value, criterion) for criterion in criteria)
        ]

    async def delete(self, instance: Model) -> None:
        collection = self._register(type(instance))
        self._store[collection].pop(instance.id, None)

    async def delete_all(self, model: type[Model]) -> None:
        self._store[self._register(model)].clear()

    def get_collection(self, model_or_name: type[Model] | str) -> InMemoryCollection:
        if isinstance(model_or_name, str):
            return self.collection_by_name(model_or_name)
        self._register(model_or_name)
        return InMemoryCollection(self, model_or_name)

    def collection_by_name(self, collection_name: str) -> InMemoryCollection:
        model = self._models_by_collection.get(collection_name)
        if model is None:
            model = next(
                (
                    candidate
                    for candidate in _all_model_subclasses(Model)
                    if getattr(candidate, "__collection__", None) == collection_name
                ),
                None,
            )
        if model is None:
            raise KeyError(f"Unknown in-memory collection: {collection_name}")
        return self.get_collection(model)

    async def close(self) -> None:
        return None

    async def ping(self) -> dict[str, Any]:
        return await self.database.command("ping")

    async def stats(self) -> dict[str, Any]:
        return await self.database.command("dbStats")

    async def _before_save(self, value: Model) -> None:
        from simstack.domain import reconcile_dataset_metadata
        from simstack.models.dataset import DataSet
        from simstack.models.dataset_metadata import DataSetMetadataTemplate
        from simstack.models.datasettuple import DataSetTuple
        from simstack.util.object_list_mixin import ObjectListMixin

        if isinstance(value, ObjectListMixin):
            for element in value._get_cache():
                await self.save(element)
        if isinstance(value, DataSet):
            template = await self.find_one(
                DataSetMetadataTemplate,
                eq("dataset_type", value.metadata.field_name),
            )
            reconciled = reconcile_dataset_metadata(
                value.metadata,
                value.collect_structure(),
                template,
            )
            await self.save(reconciled)
            for section in value.sections.values():
                cache = section._get_cache()
                for row in cache.values():
                    for model in row.values():
                        await self.save(model)
                section.data = {
                    name: {key: model.id for key, model in row.items()}
                    for name, row in cache.items()
                }
                section.column_defs = await section.make_column_defs()
                section.table_entries = await section.make_table_entries()
        if isinstance(value, DataSetTuple):
            template = await self.find_one(
                DataSetMetadataTemplate,
                eq("dataset_type", value.metadata.field_name),
            )
            reconciled = reconcile_dataset_metadata(
                value.metadata,
                value.collect_structure(),
                template,
                dataset_tuple=True,
            )
            await self.save(reconciled)
            for section in value.sections.values():
                cache = section._get_cache()
                for model_group in cache:
                    for model in model_group:
                        await self.save(model)
                section.data = [
                    [model.id for model in model_group]
                    for model_group in cache
                ]
                section.column_defs = await section.make_column_defs()
                section.table_entries = await section.make_table_entries()

    async def _after_load(self, value: Model | EmbeddedModel) -> None:
        from simstack.models.dataset import DataSet
        from simstack.models.datasettuple import DataSetTuple
        from simstack.util.importer import import_class_by_name
        from simstack.util.object_list_mixin import ObjectListMixin
        from simstack.models.node_registry import NodeRegistry

        if isinstance(value, NodeRegistry):
            await self._after_load(value.info_files)

        if isinstance(value, ObjectListMixin):
            cache = value._get_cache()
            missing = [
                object_id
                for object_id in value.elements
                if not any(item.id == object_id for item in cache)
            ]
            if missing:
                loaded = await self.find(
                    value._get_model_class(), in_values("id", missing)
                )
                by_id = {item.id: item for item in [*cache, *loaded]}
                value._set_cache([by_id[item] for item in value.elements])
        if isinstance(value, DataSet):
            for section in value.sections.values():
                cache: dict[str, dict[str, Model]] = {}
                for row_name, row in section.data.items():
                    cache[row_name] = {}
                    for key, model_id in row.items():
                        model_class = await import_class_by_name(
                            section.model_types[key], self
                        )
                        instance = await self.find_one(
                            model_class, eq("id", model_id)
                        )
                        if instance is None:
                            raise ValueError(
                                f"Missing {model_class.__name__} {model_id}"
                            )
                        cache[row_name][key] = instance
                section._set_cache(cache)
        if isinstance(value, DataSetTuple):
            for section in value.sections.values():
                cache: list[tuple[Model, ...]] = []
                for model_ids in section.data:
                    group: list[Model] = []
                    for model_type, model_id in zip(
                        section.model_types,
                        model_ids,
                    ):
                        model_class = await import_class_by_name(model_type, self)
                        instance = await self.find_one(
                            model_class,
                            eq("id", model_id),
                        )
                        if instance is None:
                            raise ValueError(
                                f"Missing {model_class.__name__} {model_id}"
                            )
                        group.append(instance)
                    cache.append(tuple(group))
                section._set_cache(cache)


def _all_model_subclasses(model: type[Model]) -> list[type[Model]]:
    children = list(model.__subclasses__())
    return children + [
        descendant
        for child in children
        for descendant in _all_model_subclasses(child)
    ]


__all__ = ["InMemoryCollection", "InMemoryRepository"]
