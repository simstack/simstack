"""Declared bulk payloads and safe, versioned codecs shared by all adapters.

A payload declaration is data in JSON Schema. Server adapters can use these
codecs without importing a workflow's Python class or executing its methods.
"""
from __future__ import annotations

import hashlib
import base64
import io
import json
import math
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from itertools import chain
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from simstack.models.document import EmbeddedModel, Field, ObjectId, Reference
from simstack.models.files import FileStack

TABLE_PREVIEW_ROWS = 20
TABLE_PREVIEW_BYTES = 32 * 1024
MAX_TABLE_ROW_BYTES = 1024 * 1024
TABLE_ROW_GROUP_BYTES = 4 * 1024 * 1024
TABLE_ROW_GROUP_ROWS = 1024


class TablePayload(EmbeddedModel):
    codec: Literal["parquet"] = "parquet"
    codec_version: Literal[1] = 1
    file: FileStack = Reference()
    row_count: int = Field(ge=0)
    columns: list[str] = Field(default_factory=list)
    logical_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_hash_version: Literal[1] = 1


def _logical_cell(value: Any):
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        return ["float", value.hex()]
    if type(value) is str:
        return ["str", value]
    if type(value) is bytes:
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if type(value) is datetime:
        return ["datetime", value.isoformat(), value.fold]
    if type(value) is date:
        return ["date", value.isoformat()]
    if type(value) is Decimal:
        sign, digits, exponent = value.as_tuple()
        return ["decimal", sign, list(digits), str(exponent)]
    if type(value) is list:
        return ["list", [_logical_cell(item) for item in value]]
    if type(value) is dict:
        return ["dict", [[key, _logical_cell(value[key])] for key in sorted(value)]]
    raise ValueError(f"Unsupported logical table value type {type(value).__name__}")


def table_logical_hash(rows: Iterable[dict[str, Any]], columns: Sequence[str] | None = None) -> str:
    """Version-one typed content identity, independent of storage/codec identity."""
    if columns is None:
        iterator = iter(rows)
        first = next(iterator, None)
        names = list(first) if first is not None else []
        rows = chain([first], iterator) if first is not None else iterator
    else:
        names = list(columns)
    digest = hashlib.sha256(b"simstack.table.logical.v1\0")
    encoded = json.dumps(names, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    for row in rows:
        _check_cell(row)
        if set(row) != set(names):
            raise ValueError("Logical table rows require the same declared keys")
        encoded = json.dumps([_logical_cell(row[name]) for name in names], ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _arrow_source(source):
    import pyarrow as pa
    return pa.BufferReader(source) if isinstance(source, (bytes, bytearray, memoryview)) else source


def _table_file(source):
    import pyarrow.parquet as pq
    parquet = pq.ParquetFile(_arrow_source(source), page_checksum_verification=True)
    metadata = parquet.schema_arrow.metadata or {}
    if metadata.get(b"simstack.payload.codec") != b"parquet-rows" or metadata.get(b"simstack.payload.version") != b"1":
        raise ValueError("Unsupported table payload codec or version")
    return parquet


def table_metadata(source) -> dict[str, Any]:
    parquet = _table_file(source)
    return {"row_count": parquet.metadata.num_rows, "columns": parquet.schema_arrow.names,
            "schema": {field.name: str(field.type) for field in parquet.schema_arrow}}


def read_table_rows(source, offset: int = 0, limit: int | None = None,
                    columns: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Read only intersecting Parquet row groups from bytes, paths or seekable IO."""
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("Table offset and limit must be nonnegative")
    parquet = _table_file(source)
    selected = list(columns) if columns is not None else None
    if selected is not None and (len(selected) != len(set(selected))
                                 or set(selected) - set(parquet.schema_arrow.names)):
        raise ValueError("Unknown or duplicate table columns")
    remaining = max(0, parquet.metadata.num_rows - offset) if limit is None else limit
    rows, start = [], 0
    for index in range(parquet.num_row_groups):
        count = parquet.metadata.row_group(index).num_rows
        if remaining == 0:
            break
        if start + count > offset:
            local_offset = max(0, offset - start)
            take = min(remaining, count - local_offset)
            group = parquet.read_row_group(index, columns=selected)
            rows.extend(group.slice(local_offset, take).to_pylist())
            remaining -= take
        start += count
    return rows


def _check_cell(value: Any) -> int:
    """Reject unsupported values before Arrow can silently coerce them."""
    if value is None or type(value) in (bool, int, float):
        return 16
    if type(value) is str:
        return len(value.encode("utf-8")) + 8
    if type(value) is bytes:
        return len(value) + 8
    if type(value) in (date, datetime, Decimal):
        return len(str(value)) + 16
    if type(value) is list:
        return 8 + sum(_check_cell(item) for item in value)
    if type(value) is dict and all(type(key) is str for key in value):
        return 8 + sum(len(key.encode("utf-8")) + _check_cell(item) for key, item in value.items())
    raise ValueError(f"Table value type {type(value).__name__} is unsupported by the Parquet rows codec")


def _same_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is float and math.isnan(left):
        return math.isnan(right)
    if type(left) is float and left == 0:
        return left == right and math.copysign(1, left) == math.copysign(1, right)
    if type(left) is Decimal:
        return left.as_tuple() == right.as_tuple()
    if type(left) is datetime:
        return left.isoformat() == right.isoformat() and left.fold == right.fold
    if type(left) is list:
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    if type(left) is dict:
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    return left == right


def encode_table_rows(rows: Sequence[dict[str, Any]], columns: Sequence[str] | None = None) -> bytes:
    """Encode typed rows losslessly; reject sparse keys and Arrow type coercion."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    names = list(columns) if columns is not None else list(rows[0]) if rows else []
    if any(type(name) is not str for name in names) or len(names) != len(set(names)):
        raise ValueError("Table columns must be unique strings")
    sizes = []
    for row in rows:
        if type(row) is not dict or set(row) != set(names):
            raise ValueError("Parquet rows require the same declared keys in every row")
        size = _check_cell(row)
        if size > MAX_TABLE_ROW_BYTES:
            raise ValueError(f"Table row exceeds {MAX_TABLE_ROW_BYTES} encoded bytes")
        sizes.append(size)
    if rows and not names:
        raise ValueError("Nonempty tables require at least one column")
    try:
        table = (pa.Table.from_pylist(list(rows), schema=None) if rows
                 else pa.table({name: pa.array([], type=pa.null()) for name in names}))
        table = table.select(names)
        if not _same_value(list(rows), table.to_pylist()):
            raise ValueError("Parquet would change table value types or missing keys")
        table = table.replace_schema_metadata({
            b"simstack.payload.codec": b"parquet-rows",
            b"simstack.payload.version": b"1",
        })
        output = io.BytesIO()
        with pq.ParquetWriter(output, table.schema, compression="zstd", write_page_checksum=True,
                              data_page_size=64 * 1024, write_batch_size=64) as writer:
            start, group_bytes = 0, 0
            for index, size in enumerate(sizes):
                if index > start and (index - start >= TABLE_ROW_GROUP_ROWS
                                      or group_bytes + size > TABLE_ROW_GROUP_BYTES):
                    writer.write_table(table.slice(start, index - start))
                    start, group_bytes = index, 0
                group_bytes += size
            writer.write_table(table.slice(start))
        return output.getvalue()
    except (pa.ArrowException, TypeError, OverflowError) as exc:
        raise ValueError("Table values are unsupported by the lossless Parquet rows codec") from exc


def table_preview(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    from simstack.models.document import encode_document_value
    result, size = [], 0
    for row in rows[:TABLE_PREVIEW_ROWS]:
        row_size = len(json.dumps(encode_document_value(row), default=str).encode("utf-8"))
        if size + row_size > TABLE_PREVIEW_BYTES:
            break
        result.append(row)
        size += row_size
    return result, len(result) < len(rows)


def _field_name(model: BaseModel, wire_name: str) -> str:
    return next((name for name, field in type(model).model_fields.items()
                 if (field.alias or name) == wire_name), wire_name)


def prepare_payload_document_fields(document: dict, spec: dict) -> dict:
    """Prepare only declared fields; targets contain typed FileStack values.

    The caller owns schema traversal, authorization, upload and persistence.
    Legacy pickle DataFrames are accepted only by this explicitly declared
    built-in compatibility codec; unknown codecs are rejected.
    """
    from simstack.models.document import decode_document_value
    value = dict(document)
    if spec.get("version") != 1 or not isinstance(spec.get("fields"), list):
        raise ValueError("Unsupported payload declaration version")
    for declaration in spec["fields"]:
        if declaration.get("codec_version") != 1:
            raise ValueError("Unsupported payload codec version")
        source, target, codec = (declaration[key] for key in ("source", "target", "codec"))
        if value.get(target) is not None:
            if value.get(source):
                raise ValueError("Payload cannot contain both inline content and a payload reference")
            continue
        content = decode_document_value(value.get(source))
        if codec == "parquet-rows":
            rows = content or []
            declared_columns = value.get("heading") if source == "row" else [
                column.get("field") if isinstance(column, dict) else column.field
                for column in value.get("columns_defs", [])]
            columns = list(rows[0]) if rows else declared_columns or []
            raw = encode_table_rows(rows, columns=columns)
            file = FileStack.from_bytes(raw, "table.parquet")
            value[target] = TablePayload(file=file, row_count=len(rows), columns=columns,
                                         logical_hash=table_logical_hash(rows, columns))
            value["preview_rows"], value["preview_truncated"] = table_preview(rows)
            value[source] = []
        elif codec == "bytes":
            if not isinstance(content, bytes):
                raise ValueError("Declared binary payload must contain bytes")
            value[target] = FileStack.from_bytes(content, declaration.get("filename", "payload.bin"))
            value[source] = b""
        elif codec in ("npy", "parquet-dataframe", "image"):
            # Built-ins are the only classes instantiated by schema-driven input.
            # Their exact fields do not import or execute workflow-defined code.
            if codec == "npy":
                from simstack.models.array_storage import ArrayStorage
                cls = ArrayStorage
            elif codec == "parquet-dataframe":
                from simstack.models.pandas_model import PandasModel
                cls = PandasModel
            else:
                from simstack.models.images2d import Image2DArtifactModel
                cls = Image2DArtifactModel
            native_fields = {field.alias or name for name, field in cls.model_fields.items()}
            native = cls.model_validate_doc({key: item for key, item in value.items() if key in native_fields})
            native.prepare_blob_payload()
            value[source] = getattr(native, _field_name(native, source))
            value[target] = getattr(native, _field_name(native, target))
            for key in ("payload_format", "dtype"):
                if key in cls.model_fields:
                    value[key] = getattr(native, key)
        else:
            raise ValueError(f"Unsupported declared payload codec {codec!r}")
    return value


def prepare_payload_model(model: BaseModel) -> None:
    fields = getattr(type(model), "__payload_fields__", ())
    if not fields:
        return
    document = {field.alias or name: object.__getattribute__(model, "__dict__")[name]
                for name, field in type(model).model_fields.items()}
    previous = None
    cached = getattr(model, "_payload_rows_cache", None)
    if getattr(type(model), "__payload_source__", None) is not None and cached is not None:
        previous = model.payload
        columns = (list(cached[0]) if cached else list(model.heading) if hasattr(model, "heading")
                   else [column.field for column in model.columns_defs])
        if previous is not None and table_logical_hash(cached, columns) == previous.logical_hash:
            return
        document[model.__payload_source__] = cached
        document["payload"] = None
    # Built-ins already have efficient typed access and explicit legacy readers.
    if all(item["codec"] in ("npy", "parquet-dataframe", "image") for item in fields):
        model.prepare_blob_payload()
        return
    prepared = prepare_payload_document_fields(document, {"version": 1, "fields": list(fields)})
    if previous is not None and previous.file.hash == prepared["payload"].file.hash:
        prepared["payload"] = previous
    for key, value in prepared.items():
        name = _field_name(model, key)
        if name in type(model).model_fields:
            setattr(model, name, value)
    if getattr(type(model), "__payload_source__", None) is not None and document.get(model.__payload_source__):
        object.__setattr__(model, "_payload_rows_cache", document[model.__payload_source__])


def prepare_payloads(roots: Sequence[Any], *, publication_namespace: str | None = None) -> None:
    """Recursively prepare declared payloads, including embedded/custom models."""
    from simstack.core.runner_execution.references import walk_runtime_values
    if publication_namespace is not None and (len(publication_namespace) != 64
            or any(char not in "0123456789abcdef" for char in publication_namespace)):
        raise ValueError("Payload publication namespace must be a SHA256 identity")
    for index, model in enumerate(list(walk_runtime_values(roots))):
        if not isinstance(model, BaseModel) or not getattr(type(model), "__payload_fields__", ()):
            continue
        before = {id(value) for value in walk_runtime_values([model]) if isinstance(value, FileStack)}
        prepare_payload_model(model)
        if publication_namespace is not None:
            for file in walk_runtime_values([model]):
                if isinstance(file, FileStack) and id(file) not in before and file.blob is None:
                    identity = f"{publication_namespace}:{index}:{file.name}:{file.hash}"
                    file.id = ObjectId(hashlib.sha256(identity.encode()).hexdigest()[:24])


class TableRowsMixin:
    """Full Python row access with an explicitly separate bounded UI preview."""
    __payload_source__: ClassVar[str]

    def __simstack_hash_inputs__(self) -> dict[str, Any]:
        """Physical location, previews and codec revisions do not change inputs."""
        source = _field_name(self, self.__payload_source__)
        excluded = {"id", source, "payload", "preview_rows", "preview_truncated"}
        fields = {name: getattr(self, name) for name in type(self).model_fields if name not in excluded}
        fields.update(self.model_extra or {})
        cached = getattr(self, "_payload_rows_cache", None)
        if self.payload is not None and cached is None:
            logical_hash = self.payload.logical_hash
        else:
            rows = cached if cached is not None else getattr(self, source)
            columns = (list(rows[0]) if rows else list(self.heading) if hasattr(self, "heading")
                       else [column.field for column in self.columns_defs])
            logical_hash = table_logical_hash(rows, columns)
        fields[self.__payload_source__] = {"logical_hash": logical_hash, "logical_hash_version": 1}
        return fields

    def _get_rows(self) -> list[dict[str, Any]]:
        source = _field_name(self, self.__payload_source__)
        if self.payload is None:
            return getattr(self, source)
        cached = getattr(self, "_payload_rows_cache", None)
        if cached is None:
            cached = read_table_rows(self.payload.file.read_bytes())
            if len(cached) != self.payload.row_count:
                raise ValueError("Table payload row count disagrees with its descriptor")
            if table_logical_hash(cached, self.payload.columns) != self.payload.logical_hash:
                raise ValueError("Table payload logical hash disagrees with its descriptor")
            object.__setattr__(self, "_payload_rows_cache", cached)
        return cached

    def _set_rows(self, rows: list[dict[str, Any]]) -> None:
        setattr(self, _field_name(self, self.__payload_source__), rows)
        self.payload = None
        self.preview_rows = []
        self.preview_truncated = False
        object.__setattr__(self, "_payload_rows_cache", None)

    def get_rows(self, offset: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self._get_rows()
        return rows[offset:] if limit is None else rows[offset:offset + limit]

    def iter_rows(self):
        return iter(self._get_rows())

    def prepare_blob_payload(self) -> None:
        # Mutating a loaded row list remains ordinary Python behavior. Rebuild
        # only when that cache was actually loaded; untouched references reuse IO.
        prepare_payload_model(self)
