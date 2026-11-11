"""Explicit scientific data operations for the Runner domain client."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import get_args
from urllib.parse import quote
from uuid import uuid4

from pydantic import BaseModel

from simstack.models.document import Model
from simstack.models.document.codec import decode_document_value, encode_document_value
from simstack.server_api.scientific_value_graph import (
    bind_publication_revisions,
    bind_read_revisions,
    decode_graph,
    encode_graph,
    graph_values,
    read_revision_baselines,
    reference_key,
    type_name,
    value_reference,
)

_materializing_values = ContextVar("simstack_materializing_values", default=None)


def contains_file_values(roots):
    """Detect file arguments without running scientific hooks or transferring bytes."""
    from simstack.models.files import FileStack

    seen = set()

    def visit(value):
        if id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, FileStack):
            return True
        if isinstance(value, BaseModel):
            children = [getattr(value, name) for name, field in type(value).model_fields.items()
                        if not field.exclude]
            children.extend((value.model_extra or {}).values())
            children.append(vars(value).get("_cache"))
        elif isinstance(value, dict):
            children = value.values()
        elif isinstance(value, (list, tuple, set, frozenset)):
            children = value
        else:
            return False
        return any(visit(child) for child in children)

    return any(visit(root) for root in roots)


def _refresh_file_metadata(graph, files):
    """Refresh only object-control fields, without rerunning scientific serializers."""
    from simstack.models.files import FileStack

    metadata = {}
    for value in files:
        base = FileStack.model_construct(**{name: getattr(value, name) for name in FileStack.model_fields})
        fields = encode_graph([base])["nodes"][0]["value"]
        names = type(value).model_json_schema()["x-simstack-file-storage-fields"]
        metadata[reference_key(type_name(type(value)), value.id)] = {
            wire_name: fields[name] for name, wire_name in names.items()}
    field_models = {node["mapping"] for node in graph["nodes"]}
    for node in graph["nodes"]:
        key = reference_key(node["mapping"], node["id"])
        if key in metadata:
            node["value"].update(metadata[key])
        for fields, value in ((True, node["value"]), (False, node.get("members"))):
            for item in graph_values(value, fields=fields, field_models=field_models):
                if item.get("__simstack_value__") == "embedded" and isinstance(item.get("value"), dict):
                    key = reference_key(item["mapping"], item["value"].get("id"))
                    if key in metadata:
                        item["value"].update(metadata[key])


class ScientificValuesAPI:
    def __init__(self, requests):
        self.requests = requests

    def _scientific_body(self, **values):
        from simstack.runner_runtime.launch_claims import current_execution

        return {
            "repository_revision": self.requests.revision(),
            "execution": current_execution(),
            **values,
        }

    async def _decode_values(self, graph, *, task_snapshots=None, repository_revision=None):
        from simstack.models.document.models import _MODEL_TYPES
        from simstack.util.importer import import_class

        types = {}
        field_models = set()

        def declared_types(annotation):
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                mapping = type_name(annotation)
                if mapping in types:
                    return
                types[mapping] = annotation
                if not annotation.__pydantic_root_model__:
                    field_models.add(mapping)
                for field in annotation.model_fields.values():
                    declared_types(field.annotation)
                if annotation.model_config.get("extra") == "allow":
                    # Pydantic has already resolved typed extras, including
                    # forward references/generics, in its validation schema.
                    # These embedded types need no independent catalogue row.
                    def schema_types(schema):
                        if isinstance(schema, dict):
                            cls = schema.get("cls")
                            if isinstance(cls, type) and issubclass(cls, BaseModel):
                                declared_types(cls)
                            for child in schema.values():
                                schema_types(child)
                        elif isinstance(schema, (list, tuple)):
                            for child in schema:
                                schema_types(child)

                    schema_types(annotation.__pydantic_core_schema__)
                # A loaded subtype is a valid value of its declared base. Do
                # not import a new module merely because a wire marker names it.
                for subtype in annotation.__subclasses__():
                    declared_types(subtype)
            else:
                for argument in get_args(annotation):
                    declared_types(argument)

        # Discover embedded value types from the owning record before resolving
        # wire markers. Plain BaseModel fields have no independent catalogue row.
        for node in graph["nodes"]:
            mapping = node["mapping"]
            declared_types(_MODEL_TYPES[mapping] if mapping in _MODEL_TYPES
                           else await import_class(mapping, self))
        tasks = {}
        for node in graph["nodes"]:
            for fields, value in ((True, node["value"]), (False, node.get("members"))):
                for item in graph_values(value, fields=fields, field_models=field_models):
                    if item.get("__simstack_value__") == "embedded":
                        mapping = item["mapping"]
                        if mapping not in types:
                            declared_types(_MODEL_TYPES[mapping] if mapping in _MODEL_TYPES
                                           else await import_class(mapping, self))
                    if item.get("__simstack_value__") != "task":
                        continue
                    identity = str(item["id"])
                    if identity not in tasks:
                        if task_snapshots is None:
                            tasks[identity] = await self.read_task_reference(identity)
                        else:
                            from simstack.server_api.workflow_tasks import decode_task_response
                            tasks[identity] = decode_task_response(task_snapshots[identity])
        values = decode_graph(
            graph, types.__getitem__, resolve_task=tasks.__getitem__
        )
        await self.materialize_references(values)
        baselines = read_revision_baselines(values, graph)
        if baselines:
            response = await self.requests.query("/values/read-baselines", {
                "values": baselines, "repository_revision": repository_revision or self.requests.revision(),
            })
            bind_read_revisions(values, response["values"])
        return values

    async def materialize_references(self, roots):
        from simstack.models.files import FileStack
        from simstack.models.dataset import DataSet, DataSetSection, DataSetSelection
        from simstack.models.datasettuple import (
            DataSetTuple,
            DataSetTupleSection,
            DataSetTupleSelection,
        )
        from simstack.util.importer import import_class_by_name
        from simstack.util.object_list_mixin import ObjectListMixin
        from simstack.methods.multiple_executor import (
            MultipleExecutorInput,
            MultipleExecutorOutput,
        )

        seen = _materializing_values.get()
        token = None
        if seen is None:
            seen = set()
            token = _materializing_values.set(seen)

        async def visit(value):
            if id(value) in seen:
                return
            seen.add(id(value))
            if isinstance(value, FileStack):
                object.__setattr__(value, "_object_requests", self.requests)
            if (
                isinstance(value, ObjectListMixin)
                and [item.id for item in (vars(value).get("_cache") or [])] != value.elements
            ):
                mapping = type_name(value._get_model_class())
                value.materialize(
                    await self.resolve_values(
                        [
                            {"mapping": mapping, "id": str(identity)}
                            for identity in value.elements
                        ]
                    )
                )
            if (
                isinstance(value, DataSetSection)
                and {name: {key: item.id for key, item in row.items()}
                     for name, row in (vars(value).get("_cache") or {}).items()} != value.data
            ):
                types = {
                    key: await import_class_by_name(name, self)
                    for key, name in value.model_types.items()
                }
                refs = [
                    {"mapping": type_name(types[key]), "id": str(identity)}
                    for row in value.data.values()
                    for key, identity in row.items()
                ]
                resolved = iter(await self.resolve_values(refs))
                value.materialize(
                    {
                        name: {key: next(resolved) for key in row}
                        for name, row in value.data.items()
                    }
                )
            if (
                isinstance(value, DataSetTupleSection)
                and [[item.id for item in row] for row in (vars(value).get("_cache") or [])] != value.data
            ):
                types = [
                    await import_class_by_name(name, self) for name in value.model_types
                ]
                refs = [
                    {"mapping": type_name(cls), "id": str(identity)}
                    for row in value.data
                    for cls, identity in zip(types, row)
                ]
                resolved = iter(await self.resolve_values(refs))
                value.materialize(
                    [tuple(next(resolved) for _ in row) for row in value.data]
                )
            if isinstance(
                value, (DataSetSelection, DataSetTupleSelection)
            ) and getattr(vars(value).get("_cache"), "id", None) != value.dataset_id:
                cls = DataSet if isinstance(value, DataSetSelection) else DataSetTuple
                value.materialize(await self.get_value(cls, value.dataset_id))
            if isinstance(value, MultipleExecutorInput) and (
                "_cache" not in vars(value)
                or [[item.id for item in row] for row in value._cache] != value.inputs_data
            ):
                refs = [
                    {"mapping": mapping, "id": str(identity)}
                    for row in value.inputs_data
                    for mapping, identity in zip(value.input_mappings, row)
                ]
                resolved = iter(await self.resolve_values(refs))
                object.__setattr__(
                    value,
                    "_cache",
                    [[next(resolved) for _ in row] for row in value.inputs_data],
                )
            if isinstance(value, MultipleExecutorOutput) and (
                "_cache" not in vars(value)
                or [item.id if item is not None else None for item in value._cache] != value.result_ids
            ):
                refs = [
                    {"mapping": value.result_mapping, "id": str(identity)}
                    for identity in value.result_ids
                    if identity is not None
                ]
                resolved = iter(await self.resolve_values(refs))
                object.__setattr__(
                    value,
                    "_cache",
                    [
                        next(resolved) if identity is not None else None
                        for identity in value.result_ids
                    ],
                )
            if isinstance(value, BaseModel):
                for name, field in type(value).model_fields.items():
                    if not field.exclude:
                        await visit(getattr(value, name))
                for child in (value.model_extra or {}).values():
                    await visit(child)
                if "_cache" in vars(value):
                    await visit(vars(value)["_cache"])
                after_resolution = getattr(value, "after_resolution", None)
                if callable(after_resolution):
                    await after_resolution()
            elif isinstance(value, dict):
                for child in value.values():
                    await visit(child)
            elif isinstance(value, (list, tuple, set, frozenset)):
                for child in value:
                    await visit(child)

        try:
            for root in roots:
                await visit(root)
        finally:
            if token is not None:
                _materializing_values.reset(token)

    async def _prepare_values(self, roots, *, upload_files=True):
        from simstack.models.dataset import DataSet, DataSetSection
        from simstack.models.datasettuple import DataSetTuple, DataSetTupleSection
        from simstack.models.files import FileStack

        seen = set()
        files = []

        async def visit(value):
            if id(value) in seen:
                return
            seen.add(id(value))
            if isinstance(value, FileStack):
                files.append(value)
                if upload_files:
                    await self._upload_files([value])
            if isinstance(value, (DataSetSection, DataSetTupleSection)):
                if "_cache" not in vars(value):
                    await self.materialize_references([value])
            if isinstance(value, BaseModel):
                prepare_publication = getattr(value, "prepare_publication", None)
                if callable(prepare_publication):
                    await prepare_publication()
            if isinstance(value, (DataSet, DataSetTuple)):
                await self.validate_dataset_metadata(
                    value.metadata,
                    value.collect_structure(),
                    dataset_tuple=isinstance(value, DataSetTuple),
                )
            if isinstance(value, BaseModel):
                for name, field in type(value).model_fields.items():
                    if not field.exclude:
                        await visit(getattr(value, name))
                for child in (value.model_extra or {}).values():
                    await visit(child)
                if "_cache" in vars(value):
                    await visit(vars(value)["_cache"])
            elif isinstance(value, dict):
                for child in value.values():
                    await visit(child)
            elif isinstance(value, (list, tuple, set, frozenset)):
                for child in value:
                    await visit(child)

        for root in roots:
            await visit(root)
        return files

    async def _upload_files(self, files):
        from simstack.util.object_transfer import ObjectFileClient

        for value in files:
            await asyncio.to_thread(value.ensure_uploaded, client=ObjectFileClient.for_api(self.requests))

    async def publish_values(self, values: list[Model]) -> list[Model]:
        from simstack.models.node_registry import NodeRegistry

        values = list(values)
        await self._prepare_values(values)
        scientific = [value for value in values if not isinstance(value, NodeRegistry)]
        if scientific:
            response = await self.requests.command(
                "/values/publications",
                self._scientific_body(graph=encode_graph(scientific)),
            )
            bind_publication_revisions(scientific, response["graph"])
            published = iter(await self._decode_values(response["graph"]))
        else:
            published = iter(())
        return [
            await self.read_task_reference(value.id)
            if isinstance(value, NodeRegistry)
            else next(published)
            for value in values
        ]

    async def publish_value(self, value: Model) -> Model:
        return (await self.publish_values([value]))[0]

    async def preview_task_inputs(self, values, *, repository_revision=None):
        """Resolve file-bearing inputs for cache lookup without publishing their bytes."""
        from simstack.models.node_registry import NodeRegistry

        values = list(values)
        files = await self._prepare_values(values, upload_files=False)
        graph = encode_graph([value for value in values if not isinstance(value, NodeRegistry)])
        _refresh_file_metadata(graph, files)
        response = await self.requests.query("/values/input-preview", {
            **self._scientific_body(graph=graph), "request_id": str(uuid4()),
            "repository_revision": repository_revision,
            "references": [value_reference(value) for value in values],
        })
        # Keep the original graph: serializing its decoded preview again would
        # apply custom serializers twice on a cache miss.
        return await self.decode_task_inputs(response), {"graph": graph, "files": files}

    async def publish_task_inputs(self, values, *, repository_revision=None, prepared=None):
        """Publish once and pin the exact accepted graph for this invocation."""
        from simstack.models.node_registry import NodeRegistry
        values = list(values)
        if prepared is None:
            await self._prepare_values(values)
            graph = encode_graph([value for value in values if not isinstance(value, NodeRegistry)])
        else:
            await self._upload_files(prepared["files"])
            graph = prepared["graph"]
            _refresh_file_metadata(graph, prepared["files"])
        request_id = str(uuid4())
        response = await self.requests.command("/values/input-publications", {
            **self._scientific_body(graph=graph),
            "repository_revision": repository_revision,
            "references": [value_reference(value) for value in values],
        }, request_id=request_id)
        bind_publication_revisions([value for value in values if not isinstance(value, NodeRegistry)], response["graph"])
        return await self.decode_task_inputs(response), request_id

    async def publish_task_outputs(self, values, *, execution=None, repository_revision=None):
        """Publish scientific edits and preserve the actual returned values separately."""
        from simstack.models.node_registry import NodeRegistry
        values = list(values)
        await self._prepare_values(values)
        request_id = str(uuid4())
        body = self._scientific_body(
            graph=encode_graph([value for value in values if not isinstance(value, NodeRegistry)]),
            references=[value_reference(value) for value in values], repository_revision=repository_revision)
        if execution is not None:
            body["execution"] = execution
        response = await self.requests.command("/values/output-publications", body, request_id=request_id)
        return await self.decode_task_inputs(response), request_id

    async def resolve_task_outputs(self, task):
        """Read the result of this computation, even after the task is rerun."""
        from simstack.server_api.workflow_tasks import WorkflowTasksAPI
        identity = {} if not hasattr(task, "id") else {
            key: value for key, value in WorkflowTasksAPI.execution(task).items()
            if key != "task_id" and value is not None}
        task_id = task.id if hasattr(task, "id") else task
        response = await self.requests.read(f"/tasks/{task_id}/outputs", **identity)
        return await self.decode_task_inputs(response)

    async def decode_task_inputs(self, response):
        from simstack.server_api.workflow_tasks import decode_task_response
        from simstack.models.node_registry import NodeRegistry
        values = iter(await self._decode_values(response["graph"], task_snapshots=response["tasks"],
                                                repository_revision=response.get("repository_revision")))
        tasks = {identity: decode_task_response(value) for identity, value in response["tasks"].items()}
        return [tasks[str(ref["id"])] if ref["mapping"] == type_name(NodeRegistry) else next(values)
                for ref in response["references"]]

    async def resolve_task_inputs(self, task_id):
        response = await self.requests.read(f"/tasks/{task_id}/inputs")
        return await self.decode_task_inputs(response)

    async def resolve_values(self, references) -> list[Model]:
        refs = []
        for reference in references:
            if isinstance(reference, dict):
                refs.append(
                    {"mapping": reference["mapping"], "id": encode_document_value(decode_document_value(reference["id"]))}
                )
            else:
                refs.append(
                    {
                        "mapping": reference.variable_mapping,
                        "id": encode_document_value(reference.reference),
                    }
                )
        if not refs:
            return []
        from simstack.models.node_registry import NodeRegistry

        task_mapping = type_name(NodeRegistry)
        scientific = {
            reference_key(ref["mapping"], ref["id"]): ref
            for ref in refs
            if ref["mapping"] != task_mapping
        }
        if scientific:
            response = await self.requests.query(
                "/values/resolve",
                {
                    "references": list(scientific.values()),
                    "repository_revision": self.requests.revision(),
                },
            )
            values = await self._decode_values(response["graph"])
        else:
            values = []
        if len(values) != len(scientific):
            from simstack.server_api.http_transport import RunnerAPIError

            raise RunnerAPIError("Server returned an incomplete scientific resolve result")
        resolved = dict(zip(scientific, values))
        for ref in refs:
            key = reference_key(ref["mapping"], ref["id"])
            if ref["mapping"] == task_mapping and key not in resolved:
                resolved[key] = await self.read_task_reference(ref["id"])
        return [resolved[reference_key(ref["mapping"], ref["id"])] for ref in refs]

    async def get_value(self, mapping: str | type, identity) -> Model:
        mapping = type_name(mapping) if isinstance(mapping, type) else mapping
        return (await self.resolve_values([{"mapping": mapping, "id": identity}]))[
            0
        ]

    async def catalogue_models(self):
        response = await self.requests.read(
            "/catalogue/models", repository_revision=self.requests.revision()
        )
        return await self._decode_values(response["graph"])

    async def catalogue_nodes(self):
        response = await self.requests.read(
            "/catalogue/nodes", repository_revision=self.requests.revision()
        )
        return await self._decode_values(response["graph"])

    async def query_records(
        self, record_type: str | type, query: str = "all", *,
        filters=None, order_by=None, skip=0, limit=None, **parameters
    ) -> list[Model]:
        if query not in {"all", "by_ids", "by_field_name", "by_name"}:
            raise ValueError("Unknown scientific record query")
        mapping = (
            type_name(record_type) if isinstance(record_type, type) else record_type
        )
        response = await self.requests.query(
            "/records/query",
            {
                "record_type": mapping,
                "query": query,
                "parameters": encode_document_value(parameters),
                "filters": encode_document_value(filters or []),
                "order_by": order_by or [],
                "skip": skip,
                "limit": limit,
                "repository_revision": self.requests.revision(),
            },
        )
        return await self._decode_values(response["graph"])

    async def archive_records(self, values: list[Model]):
        return await self.requests.command(
            "/records/archive",
            self._scientific_body(
                **{
                    "references": [
                        value_reference(value)
                        for value in values
                    ],
                }
            ),
        )

    async def validate_dataset_metadata(
        self, metadata, structure, *, dataset_tuple=False, freeze=False
    ):
        response = await self.requests.command(
            "/datasets/metadata/validate",
            self._scientific_body(
                **{
                    "metadata": encode_document_value(
                        metadata.model_dump(mode="python")
                    ),
                    "structure": structure,
                    "dataset_tuple": dataset_tuple,
                    "freeze": freeze,
                }
            ),
        )
        canonical = decode_document_value(response["metadata"])
        metadata.structure = canonical["structure"]
        metadata.is_validated = canonical.get("is_validated", metadata.is_validated)
        return response["valid"]

    async def artifact_mappings(self, call_path: str | None):
        if call_path is None:
            return []
        response = await self.requests.read(
            "/catalogue/artifacts",
            call_path=call_path,
            repository_revision=self.requests.revision(),
        )
        return await self._decode_values(response["graph"])

    async def register_artifact_mapping(self, mapping):
        response = await self.requests.command(
            "/catalogue/artifacts", self._scientific_body(graph=encode_graph([mapping]))
        )
        return (await self._decode_values(response["graph"]))[0]

    async def query_artifacts(
        self, record_type, *, parent_id=None, name=None, path=None, artifact_type=None
    ):
        mapping = (
            type_name(record_type) if isinstance(record_type, type) else record_type
        )
        response = await self.requests.query(
            "/artifacts/query",
            {
                "record_type": mapping,
                "parent_id": str(parent_id) if parent_id is not None else None,
                "name": name,
                "path": path,
                "artifact_type": artifact_type,
                "repository_revision": self.requests.revision(),
            },
        )
        return await self._decode_values(response["graph"])

    async def clear_generated_artifacts(self, task):
        return await self.requests.command(
            f"/tasks/{quote(str(task.id), safe='')}/artifacts/clear-generated",
            self._scientific_body(task_generation=getattr(task, "_execution_generation", None)),
        )

    async def replace_artifacts(self, task, artifact_ids):
        return await self.requests.command(
            f"/tasks/{quote(str(task.id), safe='')}/artifacts/replace",
            self._scientific_body(
                **{
                    "task_generation": getattr(task, "_execution_generation", None),
                    "artifact_ids": list(map(str, artifact_ids)),
                }
            ),
        )

    async def publish_progress_files(self, task_id, files):
        files = list(files)
        await self._prepare_values(files)
        return await self.requests.command(
            f"/tasks/{quote(str(task_id), safe='')}/progress-files",
            self._scientific_body(graph=encode_graph(files)),
        )

    async def read_task_reference(self, task_id):
        from simstack.server_api.workflow_tasks import decode_task_response

        response = await self.requests.read(f"/tasks/{task_id}")
        value = decode_task_response(response["task"])
        await self.materialize_references([value])
        return value
