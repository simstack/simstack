"""Compact dataset section snapshots and persistence-neutral runtime access."""

from __future__ import annotations

import hashlib
import json
import base64
import math
import copy
from datetime import date, datetime, timezone
from decimal import Decimal
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Literal, Protocol

from simstack.models.document import EmbeddedModel, Field, Model, ObjectId
from simstack.models.payloads import TablePayload


class SharedDatasetSectionStorage(EmbeddedModel):
    version: Literal[1] = 1
    dataset_id: ObjectId
    section_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0)
    projection: TablePayload | None = None
    projection_version: Literal[1] = 1


DatasetSectionStorage = SharedDatasetSectionStorage


@dataclass(frozen=True, slots=True)
class DatasetModelRow:
    """One snapshot row with hydrated models and stable membership identity."""

    row_id: str
    position: int
    name: str | None
    models: dict[str, Model] | tuple[Model, ...]


@dataclass(frozen=True, slots=True)
class DatasetModelPage:
    """Typed rows from a pinned snapshot or an unpublished working buffer."""

    rows: tuple[DatasetModelRow, ...]
    revision: str | None
    row_count: int
    offset: int
    next_offset: int | None

    def __iter__(self) -> Iterator[DatasetModelRow]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


class DatasetIOProvider(Protocol):
    def page(
        self,
        storage: SharedDatasetSectionStorage,
        *,
        kind: str,
        section_key: str,
        offset: int,
        limit: int,
    ) -> dict: ...
    def hydrate_documents(self, entries: list[dict]) -> dict[str, object]: ...


_dataset_io = ContextVar("simstack_dataset_io", default=None)


@contextmanager
def dataset_io_scope(provider: DatasetIOProvider):
    token = _dataset_io.set(provider)
    try:
        yield provider
    finally:
        _dataset_io.reset(token)


def dataset_io_provider():
    provider = _dataset_io.get()
    if provider is not None:
        return provider
    from simstack.core.context import context

    if context.initialized and context.runner_mode:
        from simstack.runner_api.datasets import DatasetRunnerClient

        return DatasetRunnerClient.from_context()
    raise RuntimeError(
        "Normalized dataset rows require an explicit scoped dataset provider"
    )


class DatasetSectionHasher:
    """Incremental form of the canonical version-one membership identity."""

    def __init__(self, model_types):
        self._digest = hashlib.sha256(b"simstack.dataset.section.v1\0")
        self._digest.update(json.dumps(model_types, sort_keys=True, separators=(",", ":")).encode())

    def update(self, row):
        # Documents and display projection codecs are not membership identity.
        semantic = {
            key: row.get(key)
            for key in ("row_id", "position", "row_name", "refs", "entries")
        }
        encoded = json.dumps(
            dataset_json_value(semantic),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        self._digest.update(len(encoded).to_bytes(8, "big"))
        self._digest.update(encoded)

    def hexdigest(self):
        return self._digest.hexdigest()


def dataset_section_revision(model_types, rows) -> str:
    """Canonical version-one membership identity shared with Server publication."""
    digest = DatasetSectionHasher(model_types)
    for row in rows:
        digest.update(row)
    return digest.hexdigest()


def dataset_root_revision(document) -> str:
    """Identity of a compact root, independent of projection file locators."""
    value = copy.deepcopy(document)
    identity = value.pop("_id", value.get("id"))
    if identity is not None:
        value["id"] = str(identity)
    value.pop("revision", None)
    for section in value.get("sections", {}).values():
        if isinstance(section, dict) and isinstance(section.get("storage"), dict):
            section["storage"].pop("projection", None)
    encoded = json.dumps(
        dataset_json_value(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def dataset_json_value(value):
    """Idempotent extended JSON for projection cells and canonical snapshot hashes."""
    if value is None or type(value) in (bool, str):
        return value
    if isinstance(value, int):
        return (
            value
            if abs(value) <= 2**53 - 1
            else {
                "$numberLong" if -(2**63) <= value < 2**63 else "$numberDecimal": str(
                    value
                )
            }
        )
    if isinstance(value, float):
        return (
            value
            if math.isfinite(value)
            else {
                "$numberDouble": "NaN"
                if math.isnan(value)
                else "Infinity"
                if value > 0
                else "-Infinity"
            }
        )
    if isinstance(value, Decimal):
        return {"$numberDecimal": str(value)}
    if isinstance(value, bytes):
        return {
            "$binary": {
                "base64": base64.b64encode(value).decode("ascii"),
                "subType": "00",
            }
        }
    if isinstance(value, (date, datetime)):
        return {"$date": value.isoformat()}
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, EmbeddedModel):
        return dataset_json_value(value.model_dump(mode="python", by_alias=True))
    if isinstance(value, dict):
        return {str(key): dataset_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [dataset_json_value(item) for item in value]
    raise ValueError(f"Unsupported dataset projection value {type(value).__name__}")


def dataset_source_hash(document):
    from simstack.models.document import decode_document_value

    value = dict(decode_document_value(document))
    if "_id" in value:
        value["id"] = str(value.pop("_id"))
    if "id" in value:
        value["id"] = str(value["id"])

    def storage_precision(item):
        if isinstance(item, datetime):
            if item.tzinfo is not None:
                item = item.astimezone(timezone.utc).replace(tzinfo=None)
            return item.replace(microsecond=item.microsecond // 1000 * 1000)
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, dict):
            return {key: storage_precision(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [storage_precision(child) for child in item]
        return item

    encoded = json.dumps(
        dataset_json_value(storage_precision(value)),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def dataset_source_graph_hash(entry: dict) -> str:
    """Version-one identity of a typed root and its flattened reference closure.

    Document ordering and wire metadata do not affect identity. Every document
    uses dataset_source_hash's BSON precision and extended JSON normalization.
    """
    def identity(document):
        if not isinstance(document, dict) or not isinstance(document.get("value"), dict):
            raise ValueError("Dataset source graph requires typed document objects")
        mapping, value = document.get("variable_mapping"), document["value"]
        if not isinstance(mapping, str) or not mapping:
            raise ValueError("Dataset source graph requires document type mappings")
        if len({"id", "_id"} & set(value)) != 1:
            raise ValueError("Dataset source graph requires one canonical document identity")
        identifier = _canonical_row_id(str(value.get("id", value.get("_id"))), label="source identity")
        if document.get("reference", identifier) != identifier:
            raise ValueError("Dataset source graph reference disagrees with its identity")
        return mapping, identifier, dataset_source_hash(value)

    root = identity(entry)
    dependencies = entry.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("Dataset source graph dependencies must be a list")
    documents = {root[1]: root}
    for dependency in dependencies:
        candidate = identity(dependency)
        if candidate[1] in documents and documents[candidate[1]] != candidate:
            raise ValueError("Dataset source graph contains conflicting document identities")
        documents[candidate[1]] = candidate
    canonical = [root, sorted(value for key, value in documents.items() if key != root[1])]
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(b"simstack.dataset.source.graph.v1\0" + encoded).hexdigest()


def _dataset_source_entry(model):
    from simstack.core.runner_execution.serialization import _dependent_models, _model_document

    def document(value):
        return {
            "variable_mapping": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _model_document(value),
        }

    return {**document(model), "dependencies": [
        {**document(value), "reference": str(value.id)} for value in _dependent_models(model)
    ]}


class SharedDatasetSelectionStorage(EmbeddedModel):
    version: Literal[1] = 1
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_count: int = Field(ge=0)


class SharedDatasetSelectionChange(EmbeddedModel):
    section_name: str
    section_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    add_row_ids: list[str] = Field(default_factory=list)
    remove_row_ids: list[str] = Field(default_factory=list)


def section_kind(section):
    return getattr(type(section), "__dataset_kind__")


def section_cache(section):
    values = object.__getattribute__(section, "__dict__")
    if values.get("_cache") is None and section.storage is not None:
        if section.storage.row_count == 0:
            empty = {} if section_kind(section) == "dict" else []
            values.update(
                _cache=empty, _dataset_row_ids=empty.copy(), _dataset_data=empty.copy()
            )
        else:
            load_section(section)
    return values.get("_cache")


def section_row_ids(section):
    values = object.__getattribute__(section, "__dict__")
    cache = section._get_cache()
    if section_kind(section) == "dict":
        ids = values.setdefault("_dataset_row_ids", {})
        for name in cache:
            ids.setdefault(name, str(ObjectId()))
    else:
        ids = values.setdefault("_dataset_row_ids", [])
        while len(ids) < len(cache):
            ids.append(str(ObjectId()))
    return ids


def section_data(section):
    if section.storage is None:
        return section.inline_data
    cache = section._get_cache()
    if section_kind(section) == "dict":
        return {
            name: {slot: model.id for slot, model in row.items()}
            for name, row in cache.items()
        }
    values = object.__getattribute__(section, "__dict__")
    return values.setdefault(
        "_dataset_data", [[model.id for model in row] for row in cache]
    )


def set_section_data(section, data):
    if section.storage is None:
        section.inline_data = data
    else:
        object.__getattribute__(section, "__dict__")["_dataset_data"] = data


def iter_section_pages(section, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Dataset page limit must be between 1 and 100")
    if section.storage is None:
        raise RuntimeError(
            "Only normalized dataset snapshots support remote membership pages"
        )
    storage = section.storage
    provider = dataset_io_provider()
    offset = 0
    while offset < storage.row_count:
        page = provider.page(
            storage,
            kind=section_kind(section),
            section_key=getattr(section, "_dataset_section_key", storage.section_id),
            offset=offset,
            limit=limit,
        )
        rows = page.get("rows")
        if (
            page.get("version") != 1
            or page.get("revision") != storage.revision
            or page.get("row_count") != storage.row_count
            or page.get("offset") != offset
            or not isinstance(rows, list)
            or not rows
            or len(rows) > limit
        ):
            raise ValueError(
                "Dataset membership page does not match its pinned snapshot"
            )
        for index, row in enumerate(rows):
            if row.get("position") != offset + index:
                raise ValueError("Dataset membership page has a gap or reordered rows")
        next_offset = offset + len(rows)
        expected = next_offset if next_offset < storage.row_count else None
        if page.get("next_offset") != expected:
            raise ValueError("Dataset membership page has an invalid continuation")
        yield page
        offset = next_offset


def load_section(section):
    values = object.__getattribute__(section, "__dict__")
    kind = section_kind(section)
    cache, ids, data = ({}, {}, {}) if kind == "dict" else ([], [], [])
    provider = dataset_io_provider()
    seen, original_rows = set(), {}
    for page in iter_section_pages(section):
        for row in page["rows"]:
            identity = row["row_id"]
            if (
                not isinstance(identity, str)
                or len(identity) != 24
                or any(char not in "0123456789abcdef" for char in identity)
            ):
                raise ValueError("Dataset row ID must be canonical")
            if identity in seen:
                raise ValueError(
                    "Dataset membership snapshot contains duplicate row IDs"
                )
            seen.add(identity)
            original_rows[identity] = {
                key: copy.deepcopy(row.get(key)) for key in ("refs", "entries")
            }
            documents = provider.hydrate_documents(row.get("documents", []))
            refs = row["refs"]
            for ref in refs:
                model = documents[str(ref["reference"])]
                if (
                    f"{type(model).__module__}.{type(model).__qualname__}"
                    != ref["variable_mapping"]
                ):
                    raise ValueError(
                        "Dataset member reference type does not match its document"
                    )
            original_rows[identity]["graph_hashes"] = {
                ref["slot"]: dataset_source_graph_hash(_dataset_source_entry(documents[str(ref["reference"])]))
                for ref in refs
            }
            if kind == "dict":
                name = row["row_name"]
                if name in cache:
                    raise ValueError("Dataset membership contains duplicate named rows")
                cache[name] = {
                    ref["slot"]: documents[str(ref["reference"])] for ref in refs
                }
                ids[name] = identity
                data[name] = {ref["slot"]: ObjectId(ref["reference"]) for ref in refs}
            else:
                refs = sorted(refs, key=lambda ref: int(ref["slot"]))
                cache.append(tuple(documents[str(ref["reference"])] for ref in refs))
                ids.append(identity)
                data.append([ObjectId(ref["reference"]) for ref in refs])
    values.update(
        _cache=cache,
        _dataset_row_ids=ids,
        _dataset_data=data,
        _dataset_original_rows=original_rows,
    )


def _canonical_row_id(value, *, label):
    if (
        not isinstance(value, str)
        or len(value) != 24
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"Dataset {label} must be a canonical ObjectId string")
    return value


def _hydrate_model_page(section, page, provider):
    kind = section_kind(section)
    result, identities, names = [], set(), set()
    for row in page["rows"]:
        identity = _canonical_row_id(row.get("row_id"), label="row ID")
        if identity in identities:
            raise ValueError("Dataset model page contains duplicate row IDs")
        identities.add(identity)
        name = row.get("row_name")
        if kind == "dict":
            if not isinstance(name, str) or name in names:
                raise ValueError("Dataset model page requires unique string row names")
            names.add(name)
        elif name is not None:
            raise ValueError("Dataset tuple rows cannot have a row name")
        refs = row.get("refs")
        if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
            raise ValueError("Dataset model row references must be a list of objects")
        by_slot = {}
        for ref in refs:
            slot = ref.get("slot")
            mapping = ref.get("variable_mapping")
            if not isinstance(slot, str) or slot in by_slot:
                raise ValueError("Dataset model row has an invalid or duplicate slot")
            if not isinstance(mapping, str) or not mapping:
                raise ValueError("Dataset model reference requires a type mapping")
            _canonical_row_id(ref.get("reference"), label="model reference")
            by_slot[slot] = ref
        expected_slots = (
            set(section.model_types)
            if kind == "dict"
            else {str(index) for index in range(len(section.model_types))}
        )
        if set(by_slot) - expected_slots or (kind == "tuple" and set(by_slot) != expected_slots):
            raise ValueError("Dataset model row slots disagree with its section types")
        documents = row.get("documents")
        if not isinstance(documents, list):
            raise ValueError("Dataset model row requires typed documents")
        document_ids, documents_by_id = set(), {}
        for entry in documents:
            value = entry.get("value") if isinstance(entry, dict) else None
            if not isinstance(value, dict) or len({"id", "_id"} & set(value)) != 1:
                raise ValueError("Dataset row document requires exactly one canonical identity")
            document_id = _canonical_row_id(value.get("id", value.get("_id")), label="document identity")
            document_ids.add(document_id)
            documents_by_id[document_id] = entry
        reference_ids = {ref["reference"] for ref in refs}
        if document_ids != reference_ids:
            raise ValueError("Dataset model row has missing or extra typed documents")
        for ref in refs:
            if ref.get("graph_hash") is not None and dataset_source_graph_hash(documents_by_id[ref["reference"]]) != ref["graph_hash"]:
                raise ValueError("Dataset model row document graph disagrees with its snapshot")
        # Identity is scoped to one row graph: the same public ModelID may have
        # different pinned versions in different rows of this snapshot.
        documents = provider.hydrate_documents(documents)
        if not isinstance(documents, dict) or set(documents) != reference_ids:
            raise ValueError("Dataset model row hydration returned missing or extra models")
        models = {}
        for slot, ref in by_slot.items():
            model = documents[ref["reference"]]
            if not isinstance(model, Model) or str(model.id) != ref["reference"]:
                raise ValueError("Dataset model identity does not match its reference")
            if f"{type(model).__module__}.{type(model).__qualname__}" != ref["variable_mapping"]:
                raise ValueError("Dataset member reference type does not match its document")
            models[slot] = model
        result.append(DatasetModelRow(
            row_id=identity, position=row["position"], name=name,
            models=models if kind == "dict" else tuple(models[str(index)] for index in range(len(section.model_types))),
        ))
    return DatasetModelPage(
        rows=tuple(result), revision=page["revision"], row_count=page["row_count"],
        offset=page["offset"], next_offset=page["next_offset"],
    )


def _iter_buffer_model_pages(section, cache, limit):
    """Read already hydrated models without a provider or a section-wide copy."""
    values = object.__getattribute__(section, "__dict__")
    if cache is None:
        if section.inline_data:
            raise RuntimeError(
                "Dataset references must be hydrated by a repository service before typed iteration"
            )
        return
    named = section_kind(section) == "dict"
    identities = values.setdefault("_dataset_row_ids", {} if named else [])
    row_count, offset, rows = len(cache), 0, []
    source = cache.items() if named else enumerate(cache)
    for position, (name, models) in enumerate(source):
        if len(cache) != row_count:
            raise RuntimeError("Dataset membership changed during typed iteration")
        if named:
            identity = identities.setdefault(name, str(ObjectId()))
        else:
            while len(identities) <= position:
                identities.append(str(ObjectId()))
            identity = identities[position]
        rows.append(DatasetModelRow(
            row_id=_canonical_row_id(identity, label="row ID"), position=position,
            name=name if named else None, models=copy.deepcopy(models),
        ))
        if len(rows) == limit:
            next_offset = position + 1
            yield DatasetModelPage(
                rows=tuple(rows), revision=None, row_count=row_count, offset=offset,
                next_offset=next_offset if next_offset < row_count else None,
            )
            rows, offset = [], next_offset
    if len(cache) != row_count:
        raise RuntimeError("Dataset membership changed during typed iteration")
    if rows:
        yield DatasetModelPage(
            rows=tuple(rows), revision=None, row_count=row_count, offset=offset, next_offset=None,
        )


def iter_section_model_pages(section, limit=100) -> Iterator[DatasetModelPage]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Dataset page limit must be between 1 and 100")
    cache = object.__getattribute__(section, "__dict__").get("_cache")
    if section.storage is None or cache is not None:
        yield from _iter_buffer_model_pages(section, cache, limit)
        return
    provider = None
    for page in iter_section_pages(section, limit):
        if provider is None:
            provider = dataset_io_provider()
        yield _hydrate_model_page(section, page, provider)


def section_rows(section, *, with_documents=False):
    from simstack.util.make_table import make_table_entries_helper
    from simstack.core.runner_execution.serialization import _typed_document_entry

    cache, ids = section._get_cache(), section_row_ids(section)
    named = section_kind(section) == "dict"
    for position, pair in enumerate(cache.items() if named else enumerate(cache)):
        name, row = pair
        slots = (
            ((slot, row.get(slot)) for slot in section.model_types)
            if named
            else ((str(index), model) for index, model in enumerate(row))
        )
        refs, entries, documents = [], [], []
        for slot, model in slots:
            if model is None:
                entries.append({})
                continue
            mapping = f"{type(model).__module__}.{type(model).__qualname__}"
            from simstack.core.runner_execution.serialization import _model_document

            document = _typed_document_entry(model) if with_documents else _dataset_source_entry(model)
            refs.append(
                {
                    "slot": slot,
                    "variable_mapping": mapping,
                    "reference": str(model.id),
                    "source_hash": dataset_source_hash(_model_document(model)),
                    "graph_hash": dataset_source_graph_hash(document),
                }
            )
            entries.append(make_table_entries_helper(model))
            if with_documents:
                documents.append(document)
        result = {
            "row_id": ids[name],
            "position": position,
            "row_name": name if named else None,
            "refs": refs,
            "entries": entries,
            "documents": documents,
        }
        original = (
            object.__getattribute__(section, "__dict__")
            .get("_dataset_original_rows", {})
            .get(ids[name])
        )
        if original is not None:
            original_refs = {ref["slot"]: ref for ref in original["refs"]}
            for ref in refs:
                previous = original_refs.get(ref["slot"], {})
                if "graph_hash" not in previous and original.get("graph_hashes", {}).get(ref["slot"]) == ref["graph_hash"]:
                    ref.pop("graph_hash")
            if original["refs"] == refs:
                result["entries"] = copy.deepcopy(original["entries"])
        yield result


class DatasetSectionMixin:
    def iter_row_pages(self, limit=100):
        return iter_section_pages(self, limit)

    def iter_model_pages(self, limit: int = 100) -> Iterator[DatasetModelPage]:
        """Read typed pages without materializing an unloaded section cache.

        Unloaded snapshots use scoped dataset access and hydrate at most
        ``limit`` rows (1..100). New or already materialized sections read their
        current hydrated buffer in bounded pages, with ``page.revision=None``
        until publication. Raw, unhydrated legacy references require repository
        hydration. Dict rows expose ``row.name`` and ``row.models[slot]``; tuple
        rows expose ``row.models`` as a tuple. Do not change membership while
        iterating a working buffer.

        This is a read-only view, not the section mutation API: Python models
        remain mutable, but edits are not attached to the section. Do not reuse
        an existing model ID for a changed immutable output; create a new model
        when producing a changed result. Use the section's ordinary methods to
        edit membership; these methods retain their existing full-load behavior.
        """
        return iter_section_model_pages(self, limit)

    def iter_models(self, limit: int = 100) -> Iterator[DatasetModelRow]:
        """Flatten iter_model_pages into typed snapshot rows with bounded IO.

        The same read-only snapshot and immutable model identity rules apply.
        """
        for page in self.iter_model_pages(limit):
            yield from page

    def __simstack_hash_inputs__(self):
        values = object.__getattribute__(self, "__dict__")
        revision = (
            self.storage.revision
            if self.storage is not None and values.get("_cache") is None
            else dataset_section_revision(self.model_types, section_rows(self))
        )
        return {"model_types": self.model_types, "membership_revision": revision}


def dataset_projection_cells(entries, model_types):
    """Version-one display projection; RFC6901 keys never collide across slots."""
    slots = (
        list(model_types)
        if isinstance(model_types, dict)
        else [str(index) for index in range(len(model_types))]
    )
    result = {}
    for slot, entry in zip(slots, entries):
        if not isinstance(entry, dict):
            raise ValueError("Dataset projection entries must be dictionaries")
        prefix = str(slot).replace("~", "~0").replace("/", "~1")
        for key, value in entry.items():
            field = str(key).replace("~", "~0").replace("/", "~1")
            result[f"/{prefix}/{field}"] = dataset_json_value(value)
    return result


def dataset_projection_columns(cells_iter):
    names = {}
    for cells in cells_iter:
        for name in cells:
            names.setdefault(name, {"field": name, "headerName": name})
    return list(names.values())


def bind_dataset_sections(roots):
    from simstack.core.runner_execution.references import walk_runtime_values

    for value in walk_runtime_values(roots):
        if hasattr(value, "sections") and getattr(
            type(value), "__dataset_kind__", None
        ) in {"dict", "tuple"}:
            for key, section in value.sections.items():
                object.__setattr__(section, "_dataset_section_key", key)


class DatasetRootMixin:
    def __simstack_hash_inputs__(self):
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if name not in {"id", "revision"}
        }
