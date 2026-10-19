"""Single-request stdio RPC for operator-sandboxed workflow interpreters.

This process has no repository, credentials or network API. Its caller must use
the configured sandbox launcher and a revision-specific dependency environment.
Python validators and artifact functions run here, not inside the HTTP server.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import FunctionType, UnionType
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from simstack.models.document import (
    Model,
    ObjectId,
    REFERENCE_SCHEMA_KEY,
    decode_document_value,
    encode_document_value,
)
from simstack.models.document.reference_values import transform_reference_values
from simstack.util.generate_ui_schema import resolve_ui_schema

from .discovery import (
    discover_workflow,
    exported_function,
    load_modules,
    model_closure,
    object_mapping,
)
from .manifest import load_manifest
from .limits import DEFAULT_INSTALLATION_LIMITS


def _native_file(value):
    from simstack.models.files import FileStack

    return FileStack.model_validate_doc(value.model_dump_doc())


def _file_identity(value) -> str:
    document = _wire_document(_native_file(value))
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class _ArtifactFileIO:
    """Only explicitly staged inputs and request-local outputs are accessible."""

    def __init__(self, root: Path, declaration: dict):
        from simstack.models.parameters import Resource

        self.workdir = root.resolve(strict=True)
        self.resource = Resource(value=declaration["resource"])
        self.max_inline_document_bytes = declaration["max_inline_document_bytes"]
        if (
            type(self.max_inline_document_bytes) is not int
            or self.max_inline_document_bytes < 1
        ):
            raise ValueError("Artifact file storage capacity must be positive")
        self._prepared = {}
        for entry in declaration["files"]:
            identifier = _identity(entry["id"])
            path = (self.workdir / entry["path"]).resolve(strict=True)
            if not path.is_relative_to(self.workdir / "inputs") or not path.is_file():
                raise ValueError(
                    "Artifact file input is outside its prepared directory"
                )
            if identifier in self._prepared:
                raise ValueError("Artifact files contain duplicate identities")
            self._prepared[identifier] = (path, entry)
            self._check(path, entry)

    @staticmethod
    def _check(path, entry):
        from simstack.util.file_hashing import hash_file

        if (
            path.stat().st_size != entry["size_bytes"]
            or hash_file(path) != entry["checksum_sha256"]
        ):
            raise ValueError("Prepared artifact file content changed")

    @property
    def staging_dir(self) -> Path:
        directory = self.workdir / "outputs"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def materialize(self, value, local_dir: Path | None) -> Path:
        from simstack.util.file_hashing import hash_file
        from simstack.runner_api.file_transfer import transfer_resource_name

        prepared = self._prepared.get(str(value.id))
        if prepared is not None:
            path, entry = prepared
            if _file_identity(value) != entry["identity"]:
                raise ValueError(
                    "Artifact FileStack identity changed after preparation"
                )
            self._check(path, entry)
            return path
        directory = (
            Path(local_dir)
            if local_dir is not None
            else self.staging_dir / str(value.id)
        ).resolve()
        if not directory.is_relative_to(self.staging_dir):
            raise ValueError("Artifact file output escapes its request directory")
        if value.in_memory:
            return value.materialize_inline(directory)
        for location in value.locations:
            if location.status != "available" or transfer_resource_name(
                location.resource
            ) != transfer_resource_name(self.resource):
                continue
            path = (self.workdir / location.path).resolve()
            if not path.is_relative_to(self.staging_dir) or not path.is_file():
                continue
            if (
                location.size_bytes is not None
                and path.stat().st_size != location.size_bytes
            ) or (
                location.checksum_sha256 is not None
                and hash_file(path) != location.checksum_sha256.lower()
            ):
                raise ValueError("Generated artifact file content changed")
            return path
        raise ValueError("Artifact FileStack was not staged for this request")


def _identity(value: Any) -> str:
    if not isinstance(value, str) or str(ObjectId(value)) != value:
        raise ValueError("Graph identities must be canonical document id strings")
    return value


def _wire_document(model: Model) -> dict[str, Any]:
    document = model.model_dump_doc()
    if "_id" not in document or "id" in document:
        raise ValueError(
            "Model serialization must contain exactly one document identity"
        )
    document["id"] = str(document.pop("_id"))
    return encode_document_value(document)


def _runtime_classes(request):
    snapshot = Path(request["source_root"]).resolve(strict=True)
    manifest = load_manifest(
        snapshot,
        max_bytes=request.get(
            "max_manifest_bytes", DEFAULT_INSTALLATION_LIMITS.max_manifest_bytes
        ),
    )
    modules = request.get("modules", manifest.workflow.modules)
    if modules != manifest.workflow.modules:
        raise ValueError("RPC modules must match the immutable manifest declaration")
    loaded = load_modules(snapshot / manifest.workflow.source_root, modules)
    seeds = [
        value
        for module in loaded
        for value in vars(module).values()
        if issubclass(type(value), type) and issubclass(value, Model) and value is not Model
    ]
    for module in loaded:
        for value in vars(module).values():
            if type(value) is FunctionType and getattr(value, "_is_node", False):
                for annotation in get_type_hints(value, include_extras=True).values():
                    from .discovery import _model_types

                    seeds.extend(
                        cls
                        for cls in _model_types(annotation)
                        if issubclass(cls, Model)
                    )
    return model_closure(seeds), loaded, manifest


def _hydrate_embedded(value, annotation, resolve, path):
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if value is None:
        return None
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        variants = [item for item in arguments if item is not type(None)]
        if len(variants) == 1:
            return _hydrate_embedded(value, variants[0], resolve, path)
        if any(
            inspect.isclass(item) and issubclass(item, BaseModel) for item in variants
        ):
            raise ValueError(
                f"Embedded model union at {path} requires an unambiguous model contract"
            )
        return value
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        aliases = {}
        for name, field in annotation.model_fields.items():
            for alias in (name, field.alias, field.serialization_alias):
                if alias:
                    aliases[alias] = (name, field)
        extras = set(value) - set(aliases)
        if extras and annotation.model_config.get("extra") != "allow":
            raise ValueError(
                f"{path} contains unknown document fields: {sorted(extras)}"
            )
        for name, field in annotation.model_fields.items():
            present = [
                alias
                for alias in dict.fromkeys(
                    (name, field.alias, field.serialization_alias)
                )
                if alias is not None and alias in value
            ]
            if len(present) > 1:
                raise ValueError(f"{path}.{name} contains conflicting field aliases")
            if not present:
                continue
            key = present[0]
            child_path = f"{path}.{name}"
            if (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                result[key] = transform_reference_values(
                    value[key], field.annotation, resolve, path=child_path
                )
            else:
                result[key] = _hydrate_embedded(
                    value[key], field.annotation, resolve, child_path
                )
        return result
    if (
        origin in (list, set, frozenset)
        and arguments
        and isinstance(value, (list, tuple))
    ):
        return [
            _hydrate_embedded(item, arguments[0], resolve, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if origin is tuple and arguments and isinstance(value, (list, tuple)):
        types = (
            (arguments[0],) * len(value)
            if len(arguments) == 2 and arguments[1] is Ellipsis
            else arguments
        )
        if len(types) != len(value):
            raise ValueError(f"Tuple at {path} has an invalid length")
        return tuple(
            _hydrate_embedded(item, item_type, resolve, f"{path}[{index}]")
            for index, (item, item_type) in enumerate(zip(value, types))
        )
    if origin in (dict, Mapping) and len(arguments) == 2 and isinstance(value, Mapping):
        return {
            key: _hydrate_embedded(item, arguments[1], resolve, f"{path}[{key!r}]")
            for key, item in value.items()
        }
    return value


def _validate_graph(request):
    classes, loaded, manifest = _runtime_classes(request)
    mappings = {}
    for metadata in request["mappings"]:
        mapping = metadata["mapping"]
        if mapping in mappings or mapping not in classes:
            raise ValueError(f"Duplicate or unavailable model mapping: {mapping}")
        model = classes[mapping]
        from simstack.core.artifacts import ARTIFACT_TYPES

        native_artifact = next(
            (base for base in model.__mro__ if base in ARTIFACT_TYPES), None
        )
        collection = (
            native_artifact.__collection__
            if native_artifact is not None
            else model.__collection__
        )
        if metadata.get("collection_name", collection) != collection:
            raise ValueError(
                "Model collection metadata does not match the source revision"
            )
        schema = metadata.get("json_schema")
        if schema is not None and json.loads(schema) != model.model_json_schema():
            raise ValueError(
                f"Model schema does not match the source revision: {mapping}"
            )
        mappings[mapping] = model
    graph = {}
    for entry in request["documents"]:
        mapping, identifier = entry["mapping"], _identity(entry["id"])
        if mapping not in mappings or (mapping, identifier) in graph:
            raise ValueError(
                "Graph contains an undeclared mapping or duplicate identity"
            )
        document = entry["document"]
        if (
            not isinstance(document, dict)
            or "_id" in document
            or document.get("id") != identifier
        ):
            raise ValueError("Graph document must contain exactly its canonical id")
        graph[mapping, identifier] = decode_document_value(document)
    roots = request["roots"]
    root_keys = [(item["mapping"], _identity(item["id"])) for item in roots]
    if (
        (not roots and (graph or request.get("operation") == "validate_graph"))
        or len(root_keys) != len(set(root_keys))
        or any(key not in graph for key in root_keys)
    ):
        raise ValueError("Graph roots must be unique existing identities")
    hydrated, active = {}, set()

    def build(key):
        if key in hydrated:
            return hydrated[key]
        if key not in graph:
            raise ValueError(f"Missing graph document: {key}")
        if key in active:
            raise ValueError("Cyclic document references cannot be hydrated")
        active.add(key)
        model = mappings[key[0]]

        def reference(value, target, path):
            identity = _identity(value)
            mapping = object_mapping(target)
            if mapping not in mappings:
                raise ValueError(f"{path} uses an undeclared reference model")
            return build((mapping, identity))

        try:
            document = _hydrate_embedded(graph[key], model, reference, key[0])
            result = model.model_validate_doc(document)
            if str(result.id) != key[1]:
                raise ValueError("Custom validation changed a document identity")
            hydrated[key] = result
            return result
        finally:
            active.remove(key)

    for key in graph:
        build(key)
    normalized = [
        {
            "mapping": mapping,
            "id": identifier,
            "document": _wire_document(hydrated[mapping, identifier]),
        }
        for mapping, identifier in graph
    ]
    return {"documents": normalized, "roots": roots}, hydrated, loaded, manifest


def _artifact_files(arguments):
    from simstack.core.artifacts import ArtifactArguments
    from simstack.models.files import FileStack

    pending, seen, files = [arguments], set(), {}
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, FileStack):
            document = _wire_document(_native_file(value))
            identity = str(value.id)
            if identity in files and files[identity][1] != document:
                raise ValueError("Artifact graph contains conflicting file identities")
            files[identity] = (value, document)
        elif isinstance(value, Mapping):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
        elif isinstance(value, (BaseModel, ArtifactArguments)):
            pending.extend(vars(value).values())
            pending.extend((getattr(value, "__pydantic_extra__", None) or {}).values())
    return list(files.values())


def _artifact_preview(value, active=None):
    """JSON-only editor arguments; never serialize compressed file contents."""
    from datetime import date, datetime
    from enum import Enum

    active = set() if active is None else active
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if id(value) in active:
        return "<circular_ref>"
    active.add(id(value))
    try:
        if isinstance(value, BaseModel):
            return _artifact_preview(value.model_dump(), active)
        if isinstance(value, Mapping):
            return {
                str(key): _artifact_preview(item, active) for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_artifact_preview(item, active) for item in value]
        return {
            key: _artifact_preview(item, active)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    finally:
        active.remove(id(value))


async def _artifact(request):
    from simstack.core.artifacts import ARTIFACT_TYPES, ArtifactArguments
    from simstack.core.simstack_result import SimstackResult
    from simstack.runner_protocol.constants import RunnerTaskStatus

    _, graph, modules, manifest = _validate_graph(request)
    values = request["arguments"]

    def resolve(identity):
        key = identity["mapping"], _identity(identity["id"])
        if key not in graph:
            raise ValueError("Artifact argument refers to an absent graph document")
        return graph[key]

    result_reference = values["result"]
    if set(result_reference) == {"mapping", "id"}:
        result = resolve(result_reference)
        if values.get("return_kind") == "bool":
            result = getattr(result, "value", None)
            if type(result) is not bool:
                raise ValueError("Artifact boolean result is malformed")
    else:
        result = SimstackResult(
            status=RunnerTaskStatus.COMPLETED,
            message=values.get("message"),
            custom_name=values.get("custom_name"),
            error_message=values.get("error"),
            info_files=[resolve(item) for item in values.get("info_files", [])],
        )
        for key, identity in result_reference.items():
            value = resolve(identity)
            if key == "files":
                result.files = list(value._get_cache())
            else:
                setattr(result, key, value)
    arguments = ArtifactArguments(
        result,
        ObjectId(_identity(values["task_id"])) if values.get("task_id") else None,
    )
    arguments.call_path = values.get("call_path")
    arguments.parent_id = arguments.task_id
    from simstack.models.parameters import Parameters

    arguments.parent_parameters = Parameters.model_validate(
        values.get("parameters", {})
    )
    arguments.custom_name = values.get("custom_name")
    for key, value in values.get("inputs", {}).items():
        if key in {
            "result",
            "task_id",
            "call_path",
            "child_artifacts",
            "parent_id",
            "parent_parameters",
            "custom_name",
        }:
            raise ValueError("Artifact input conflicts with reserved argument metadata")
        setattr(arguments, key, resolve(value))
    for entry in values.get("child_artifacts", []):
        candidates = {object_mapping(cls): cls for cls in ARTIFACT_TYPES}
        cls = candidates.get(entry.get("artifact_type", entry["mapping"]))
        if cls is None:
            raise ValueError("Child artifact has an unsupported native artifact type")
        key = (entry["mapping"], _identity(entry["document"]["id"]))
        if key in graph:
            value = graph[key]
            if not isinstance(value, cls):
                raise ValueError("Child artifact does not match its native type")
            arguments.child_artifacts.append(value)
            continue
        # Recorded native collection metadata is authoritative. Preserve custom
        # fields even when a child belongs to a different installed package.
        from pydantic import ConfigDict, create_model

        module_name, _, class_name = entry["mapping"].rpartition(".")
        view = create_model(
            class_name,
            __module__=module_name,
            __base__=cls,
            __config__=ConfigDict(extra="allow"),
        )
        arguments.child_artifacts.append(view.model_validate_doc(entry["document"]))
    if request["operation"] == "artifact_arguments":
        return {"arguments": _artifact_preview(arguments)}
    if request["operation"] == "artifact_files":
        return {"files": [document for _, document in _artifact_files(arguments)]}
    if request["operation"] != "artifact_code":
        mapping = request["function_mapping"]
        if mapping not in {item.function_mapping for item in manifest.artifacts}:
            raise ValueError(
                "Artifact collector is not declared by this workflow revision"
            )
        function = exported_function(modules, mapping)
    from simstack.core.file_io import file_io_scope

    provider = (
        _ArtifactFileIO(Path(request["files_root"]), request["file_io"])
        if request.get("file_io") is not None
        else None
    )
    with file_io_scope(provider) if provider is not None else contextlib.nullcontext():
        if request["operation"] == "artifact_code":
            from simstack.util.safe_code_executor import safe_code_executor

            reply = await asyncio.to_thread(
                safe_code_executor, request["function_code"], arguments
            )
            if not reply.get("success"):
                raise ValueError(reply.get("error", "Artifact code execution failed"))
            output = reply.get("result")
        else:
            output = function(arguments)
        if inspect.isawaitable(output):
            output = await output
    artifacts = (
        [] if output is None else output if isinstance(output, list) else [output]
    )
    for value in artifacts:
        if not isinstance(value, ARTIFACT_TYPES):
            raise ValueError("Artifact collector returned a non-native artifact model")
    files = []
    for value, document in _artifact_files(artifacts):
        entry = {"document": document}
        if provider is not None:
            from simstack.util.file_hashing import hash_file

            path = provider.materialize(value, None)
            entry.update(
                size_bytes=path.stat().st_size, checksum_sha256=hash_file(path)
            )
            if str(value.id) not in provider._prepared:
                entry["path"] = str(path.relative_to(provider.workdir))
        elif not value.in_memory:
            raise ValueError("File-backed artifacts require scoped file I/O")
        files.append(entry)
    return {
        "files": files,
        "artifacts": [
            {
                "mapping": object_mapping(type(value)),
                "artifact_type": object_mapping(
                    next(base for base in type(value).__mro__ if base in ARTIFACT_TYPES)
                ),
                "collection_name": str(
                    next(
                        base for base in type(value).__mro__ if base in ARTIFACT_TYPES
                    ).__collection__
                ),
                "document": _wire_document(value),
            }
            for value in artifacts
        ],
    }


def execute_request(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    if operation == "discover":
        return discover_workflow(
            request["source_root"],
            workflow_revision=request.get("workflow_revision"),
            max_manifest_bytes=request.get(
                "max_manifest_bytes", DEFAULT_INSTALLATION_LIMITS.max_manifest_bytes
            ),
        )
    if operation == "validate_graph":
        return _validate_graph(request)[0]
    if operation == "ui_schema":
        classes, _, _ = _runtime_classes(request)
        model = classes.get(request["mapping"])
        if model is None:
            raise ValueError(
                "UI schema model is absent from declared modules and their reference closure"
            )
        schema = resolve_ui_schema(model)
        return {
            "mapping": request["mapping"],
            "json_schema": model.model_json_schema(),
            "ui_schema": schema,
        }
    if operation in {
        "artifact",
        "artifact_files",
        "artifact_arguments",
        "artifact_code",
    }:
        return asyncio.run(_artifact(request))
    raise ValueError(f"Unsupported workflow runtime operation: {operation!r}")


def main() -> None:
    # Preserve a protocol-only stdout even if imports/validators print. Standard
    # stderr is bounded/redacted by the parent process supervisor.
    protocol_output = sys.stdout
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("Workflow RPC request must be a JSON object")
        with contextlib.redirect_stdout(sys.stderr):
            result = execute_request(request)
        response = {"ok": True, "result": result}
        encoded = json.dumps(response, allow_nan=False)
    except Exception as exc:
        encoded = json.dumps(
            {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        )
        print(encoded, file=protocol_output)
        raise SystemExit(1) from None
    print(encoded, file=protocol_output)


if __name__ == "__main__":
    main()
