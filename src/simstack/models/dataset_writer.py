"""Disk-backed writers for compact, claim-scoped dataset output snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Protocol, Sequence

from simstack.models.dataset_storage import (
    DatasetSectionHasher, SharedDatasetSectionStorage, _canonical_row_id,
    dataset_json_value, dataset_root_revision, dataset_source_graph_hash, dataset_source_hash,
)
from simstack.models.document import Model
from simstack.runner_api.datasets import MAX_DATASET_PAGE_BYTES


class DatasetWriteProvider(Protocol):
    """Explicit write boundary; implementations publish blobs before encoding rows."""

    staging_dir: Path

    def publish_models(self, models: Sequence[Model]) -> list[dict]: ...

    def snapshot(self, dataset_id, section_key, *, kind: str, source: Path, header: dict) -> dict: ...


_write_provider: ContextVar[DatasetWriteProvider | None] = ContextVar("simstack_dataset_write", default=None)


@contextmanager
def dataset_write_scope(provider: DatasetWriteProvider):
    """Install an explicit writer for local/server work, never a Runner fallback."""
    from simstack.core.context import context

    if context.initialized and context.runner_mode:
        raise RuntimeError("Dataset write providers cannot replace an API runner's write boundary")
    token = _write_provider.set(provider)
    try:
        yield provider
    finally:
        _write_provider.reset(token)


class _RunnerWriteProvider:
    def __init__(self):
        from simstack.core.context import context
        from simstack.core.runner_execution.state import current_runner_claim
        from simstack.runner_api.datasets import DatasetRunnerClient
        from simstack.runner_runtime.config import runner_task_workdir

        if not context.initialized or not context.runner_mode or not getattr(context.config, "dataset_storage", False):
            raise RuntimeError("Dataset writers require dataset_storage and an active Runner claim or an explicit write provider")
        self.client = DatasetRunnerClient.from_context()
        claim = current_runner_claim()
        self.staging_dir = runner_task_workdir(Path(context.config.workdir), claim.task_id)

    def publish_models(self, models):
        from simstack.core.runner_execution.blobs import publish_runtime_blobs
        from simstack.core.runner_execution.serialization import _typed_document_entry

        publish_runtime_blobs(models)
        return [_typed_document_entry(model) for model in models]

    def snapshot(self, dataset_id, section_key, *, kind, source, header):
        return self.client.snapshot(dataset_id, section_key, kind=kind, source=source, header=header)


def _type_name(value):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, type) and issubclass(value, Model):
        return f"{value.__module__}.{value.__qualname__}"
    raise TypeError("Dataset model types must be nonempty names or Model classes")


def _mapping(model):
    return f"{type(model).__module__}.{type(model).__qualname__}"


class DatasetWriter:
    """Stream rows to disk and attach compact snapshots on successful exit only.

    The root must be new (revision=None, sections={}). Rows are serialized and
    published synchronously; the writer retains no model objects. Disk usage
    grows with output size, while RAM holds one row plus section metadata.
    Nested datasets and native payloads are published by the write provider.
    Failed writes poison the context, even if their exception is caught inside
    the with block. Already staged server snapshots remain private to the claim.
    """

    def __init__(self, dataset):
        self.dataset = dataset
        self.kind = getattr(type(dataset), "__dataset_kind__", None)
        if self.kind not in {"dict", "tuple"}:
            raise TypeError("DatasetWriter requires DataSet or DataSetTuple")
        self._sections = {}
        self._state = "new"
        self._workspace = self._database = self._provider = None

    def __enter__(self):
        if self._state != "new":
            raise RuntimeError("Dataset writer contexts cannot be reused")
        self._require_empty_root()
        self._identity = str(self.dataset.id)
        try:
            self._provider = _write_provider.get() or _RunnerWriteProvider()
            directory = Path(self._provider.staging_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self._workspace = tempfile.TemporaryDirectory(prefix="dataset-writer-", dir=directory)
            self._database = sqlite3.connect(Path(self._workspace.name) / "rows.sqlite")
            self._database.execute("PRAGMA cache_size=-64")
            self._database.execute("PRAGMA temp_store=FILE")
            self._database.execute("CREATE TABLE identities (section TEXT NOT NULL, row_id TEXT NOT NULL, name TEXT, position INTEGER NOT NULL, PRIMARY KEY(section,row_id), UNIQUE(section,name), UNIQUE(section,position))")
            self._state = "open"
        except BaseException:
            self._abort()
            raise
        return self

    def _require_empty_root(self):
        if self.dataset.revision is not None or self.dataset.sections:
            raise ValueError("Dataset writer requires a new root with no existing sections; copy rows into a new dataset explicitly")

    def _require_open(self):
        if self._state != "open":
            raise RuntimeError("Dataset writer is closed or failed")

    def section(self, name: str, *, model_types=None, column_defs=None):
        """Return a dict add_row sink or a tuple append sink (both synchronous).

        Omitted dict types are inferred and frozen by the first nonempty row. Declare optional
        dict slots up front when the first row may omit them. Repeated access
        returns the same sink; conflicting explicit metadata is rejected.
        """
        self._require_open()
        try:
            if not isinstance(name, str) or not name:
                raise ValueError("Dataset section names must be nonempty strings")
            existing = self._sections.get(name)
            if existing is not None:
                if model_types is not None and existing.model_types != existing._normalize_types(model_types):
                    raise ValueError("Dataset section already has different model_types")
                if column_defs is not None and existing.column_defs != column_defs:
                    raise ValueError("Dataset section already has different column_defs")
                return existing
            cls = DatasetDictWriter if self.kind == "dict" else DatasetTupleWriter
            section = cls(self, name, model_types, column_defs)
            self._sections[name] = section
            return section
        except BaseException:
            self._abort()
            raise

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self._abort()
            return False
        self._require_open()
        try:
            self._require_empty_root()
            if str(self.dataset.id) != self._identity:
                raise RuntimeError("Dataset identity changed while its writer was active")
            prepared = {}
            for name, sink in self._sections.items():
                header = sink._header()
                sink._close_file(sync=True)
                reply = self._provider.snapshot(self.dataset.id, name, kind=self.kind, source=sink._source, header=header)
                prepared[name] = sink._compact_section(reply)
            self._require_empty_root()
            if str(self.dataset.id) != self._identity:
                raise RuntimeError("Dataset identity changed during publication")
            candidate = self.dataset.model_copy(update={"sections": prepared})
            revision = dataset_root_revision(candidate.model_dump_doc())
            # Local IO must finish before attaching any staged snapshots. A
            # failed close is a failed writer, even after the server accepted
            # every section.
            self._cleanup()
            state = object.__getattribute__(self.dataset, "__dict__")
            previous = {key: state[key] for key in ("sections", "revision")}
            try:
                self.dataset.sections = prepared
                self.dataset.revision = revision
            except BaseException:
                state.update(previous)
                raise
            self._state = "closed"
        except BaseException:
            self._abort()
            raise
        finally:
            self._cleanup(suppress_errors=self._state == "failed")
        return False

    def _abort(self):
        self._state = "failed"
        self._cleanup(suppress_errors=True)

    def _cleanup(self, *, suppress_errors=False):
        first_error = None

        def attempt(action):
            nonlocal first_error
            try:
                action()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc

        for section in self._sections.values():
            attempt(lambda: section._close_file(sync=False))
        database, self._database = self._database, None
        if database is not None:
            attempt(database.close)
        workspace, self._workspace = self._workspace, None
        if workspace is not None:
            attempt(workspace.cleanup)
        if first_error is not None and not suppress_errors:
            raise first_error


class _SectionWriter:
    def __init__(self, writer, name, model_types, column_defs):
        self._writer, self.name = writer, name
        namespace = f"simstack.dataset.writer.section.v1\0{writer._identity}:{name}"
        self.section_id = hashlib.sha256(namespace.encode()).hexdigest()[:24]
        self.model_types = self._normalize_types(model_types) if model_types is not None else None
        self.column_defs = copy.deepcopy(column_defs) if column_defs is not None else []
        if not isinstance(self.column_defs, list) or any(not isinstance(item, dict) for item in self.column_defs):
            raise TypeError("Dataset column_defs must be a list of objects")
        self.row_count = 0
        self._mappings = {}
        self._digest = DatasetSectionHasher(self.model_types) if self.model_types is not None else None
        self._source = Path(writer._workspace.name) / f"{self.section_id}.ndjson"
        self._file = self._source.open("wb")

    def _normalize_types(self, model_types):
        if self._writer.kind == "dict":
            if not isinstance(model_types, dict) or any(not isinstance(key, str) for key in model_types):
                raise TypeError("Dictionary section model_types must map string slots to model types")
            return {key: _type_name(value) for key, value in model_types.items()}
        if not isinstance(model_types, (list, tuple)):
            raise TypeError("Tuple section model_types must be an ordered list of model types")
        return [_type_name(value) for value in model_types]

    def _validate_models(self, models):
        named = self._writer.kind == "dict"
        if named:
            if not isinstance(models, dict) or any(not isinstance(key, str) for key in models):
                raise TypeError("Dataset add_row requires a dictionary with string slots")
            if any(value is not None and not isinstance(value, Model) for value in models.values()):
                raise TypeError("Dataset row values must be Model instances or None")
            present = {slot: value for slot, value in models.items() if value is not None}
            inferred = {slot: type(value).__name__ for slot, value in present.items()}
        else:
            if not isinstance(models, tuple) or any(not isinstance(value, Model) for value in models):
                raise TypeError("Dataset append requires a tuple of Model instances")
            present = {str(index): value for index, value in enumerate(models)}
            inferred = [type(value).__name__ for value in models]
        if self.model_types is None and (present or not named):
            self.model_types = inferred
            self._digest = DatasetSectionHasher(self.model_types)
            self._flush_empty_rows()
        if self.model_types is None:
            return present, []
        expected = self.model_types if named else {str(index): value for index, value in enumerate(self.model_types)}
        if set(present) - set(expected) or (not named and len(present) != len(expected)):
            raise ValueError("Dataset row slots disagree with model_types; declare optional slots before the first row")
        for slot, model in present.items():
            mapping = _mapping(model)
            if expected[slot] not in {type(model).__name__, type(model).__qualname__, mapping}:
                raise ValueError("Dataset row model type disagrees with its declared slot")
            if slot in self._mappings and self._mappings[slot] != mapping:
                raise ValueError("Dataset row slot changed its canonical model type")
            self._mappings[slot] = mapping
        return present, list(expected)

    def _add(self, models, *, name, row_id):
        self._writer._require_open()
        try:
            present, slots = self._validate_models(models)
            if name is not None and not isinstance(name, str):
                raise TypeError("Dataset row names must be strings")
            identity = row_id if row_id is not None else hashlib.sha256(
                f"simstack.dataset.writer.row.v1\0{self.section_id}:{self.row_count}:{name}".encode()
            ).hexdigest()[:24]
            _canonical_row_id(identity, label="row ID")
            if self._writer.kind == "dict" and name is None:
                name = identity
            try:
                self._writer._database.execute("INSERT INTO identities(section,row_id,name,position) VALUES(?,?,?,?)", (self.name, identity, name, self.row_count))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Dataset writer contains a duplicate row name or row ID") from exc
            if self.model_types is None:
                # Initial empty dict rows carry no models; keep only their keys
                # on disk until the first nonempty row fixes projection slots.
                self.row_count += 1
                if self.row_count % 100 == 0:
                    self._writer._database.commit()
                return identity
            values = [present[slot] for slot in slots if slot in present]
            documents = self._writer._provider.publish_models(values)
            if not isinstance(documents, list) or len(documents) != len(values):
                raise ValueError("Dataset write provider returned missing or extra row documents")
            from simstack.util.make_table import make_table_entries_helper

            refs, entries, index = [], [], 0
            unique_documents, graph_hashes = {}, {}
            for slot in slots:
                model = present.get(slot)
                if model is None:
                    entries.append({})
                    continue
                document = documents[index]
                index += 1
                value = document.get("value", {})
                if document.get("variable_mapping") != _mapping(model) or value.get("id", value.get("_id")) != str(model.id):
                    raise ValueError("Dataset write provider changed row model identity or type")
                graph_hash = dataset_source_graph_hash(document)
                key = str(model.id)
                if key in graph_hashes and graph_hashes[key] != graph_hash:
                    raise ValueError("Dataset row contains conflicting model identities")
                graph_hashes[key] = graph_hash
                unique_documents.setdefault(key, document)
                refs.append({"slot": slot, "variable_mapping": _mapping(model), "reference": str(model.id),
                             "source_hash": dataset_source_hash(value), "graph_hash": graph_hash})
                entries.append(make_table_entries_helper(model))
            row = {"row_id": identity, "position": self.row_count, "row_name": name,
                   "refs": refs, "entries": entries, "documents": list(unique_documents.values())}
            self._write_row(row)
            self.row_count += 1
            if self.row_count % 100 == 0:
                self._writer._database.commit()
            return identity
        except BaseException:
            self._writer._abort()
            raise

    def _write_row(self, row):
        encoded = json.dumps(dataset_json_value(row), separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(encoded) > MAX_DATASET_PAGE_BYTES:
            raise ValueError("Dataset membership row exceeds its staging byte budget")
        self._file.write(encoded)
        self._digest.update(row)

    def _flush_empty_rows(self):
        slots = len(self.model_types)
        cursor = self._writer._database.execute(
            "SELECT row_id,name,position FROM identities WHERE section=? ORDER BY position", (self.name,),
        )
        try:
            for identity, name, position in cursor:
                self._write_row({"row_id": identity, "position": position, "row_name": name,
                                 "refs": [], "entries": [{} for _ in range(slots)], "documents": []})
        finally:
            cursor.close()

    def _close_file(self, *, sync):
        if not self._file.closed:
            if sync:
                self._file.flush()
                os.fsync(self._file.fileno())
            self._file.close()

    def _header(self):
        if self.model_types is None:
            self.model_types = {} if self._writer.kind == "dict" else []
            self._digest = DatasetSectionHasher(self.model_types)
            self._flush_empty_rows()
        self._writer._database.commit()
        header = {"version": 1, "section_id": self.section_id, "model_types": self.model_types,
                  "column_defs": self.column_defs, "expected_revision": None}
        if len(json.dumps(header, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) + 1 > MAX_DATASET_PAGE_BYTES:
            raise ValueError("Dataset snapshot header exceeds its byte budget")
        return header

    def _compact_section(self, reply):
        storage = SharedDatasetSectionStorage.model_validate(reply["storage"])
        if (str(storage.dataset_id) != self._writer._identity or storage.section_id != self.section_id
                or storage.row_count != self.row_count or storage.revision != self._digest.hexdigest()):
            raise ValueError("Dataset snapshot response does not match its spooled membership")
        columns = reply.get("column_defs")
        if not isinstance(columns, list) or any(not isinstance(item, dict) for item in columns):
            raise ValueError("Dataset snapshot response has invalid column metadata")
        from simstack.models.dataset import DataSetSection
        from simstack.models.datasettuple import DataSetTupleSection

        cls = DataSetSection if self._writer.kind == "dict" else DataSetTupleSection
        result = cls(model_types=self.model_types, column_defs=columns, storage=storage)
        object.__setattr__(result, "_dataset_section_key", self.name)
        return result


class DatasetDictWriter(_SectionWriter):
    def add_row(self, models: dict[str, Model | None], name: str | None = None, *, row_id: str | None = None) -> str:
        """Publish one named/sparse model row, returning its stable row ID."""
        return self._add(models, name=name, row_id=row_id)


class DatasetTupleWriter(_SectionWriter):
    def append(self, models: tuple[Model, ...], *, row_id: str | None = None) -> str:
        """Publish one ordered model group synchronously, returning its row ID."""
        return self._add(models, name=None, row_id=row_id)
