from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import simstack.core.runner_execution.lifecycle as runner_lifecycle
import simstack.core.runner_execution.mappings as runner_mappings
import simstack.core.runner_execution.nested as runner_nested
from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.node import node
from simstack.core.node import compute_arg_hash
from simstack.core.node_code_version import node_code_version
from simstack.core.runner_execution import (
    RunnerTaskExecutionError,
    RunnerTaskPayloadError,
    execute_claimed_task,
    hydrate_task_inputs,
    hydrate_task_results,
    node_registry_from_task,
    serialize_runner_result,
)
from simstack.models.document import Model, encode_document_value
from simstack.models.file_list import FileListModel
from simstack.models.file_instance import FileInstance
from simstack.models.files import FileStack
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters, Resource
from simstack.runner_api import ResolvedChildTask
from simstack.runner_protocol import RUNNER_TASK_WIRE_FIELDS


def _patch_runner_context(monkeypatch, fake_context) -> None:
    monkeypatch.setattr(runner_lifecycle, "context", fake_context)
    monkeypatch.setattr(runner_mappings, "context", fake_context)
    monkeypatch.setattr(runner_nested, "context", fake_context)


class BinaryInput(Model):
    payload: bytes


class BinaryOutput(Model):
    payload: bytes


BinaryInputAlias = BinaryInput


async def api_root_node(value: BinaryInput, **_kwargs) -> BinaryOutput:
    return BinaryOutput(payload=value.payload + b"-done")


def api_increment_node(value: BinaryInput) -> BinaryOutput:
    return BinaryOutput(payload=str(int(value.payload) + 1).encode())


def api_task_id_model_input(task_id: BinaryInput) -> BinaryOutput:
    return BinaryOutput(payload=task_id.payload)


def api_node_runner_positional_input(node_runner: BinaryInput, /) -> BinaryOutput:
    return BinaryOutput(payload=node_runner.payload)


def api_task_id_input_with_metadata(*, task_id: BinaryInput, **kwargs) -> BinaryOutput:
    assert "task_id" not in kwargs
    assert isinstance(kwargs["node_runner"], runner_lifecycle.NodeRunner)
    assert isinstance(kwargs["parent_parameters"], Parameters)
    return BinaryOutput(payload=task_id.payload)


def api_node_runner_positional_with_metadata(
    node_runner: BinaryInput, /, **kwargs
) -> BinaryOutput:
    assert "node_runner" not in kwargs
    assert kwargs["task_id"] == kwargs["parent_id"]
    assert isinstance(kwargs["parent_parameters"], Parameters)
    return BinaryOutput(payload=node_runner.payload)


def api_metadata_named_input_via_kwargs(**kwargs) -> BinaryOutput:
    inputs = [value for value in kwargs.values() if isinstance(value, BinaryInput)]
    assert len(inputs) == 1
    return BinaryOutput(payload=inputs[0].payload)


@node
async def api_plain_async_child(value: BinaryInput) -> BinaryOutput:
    raise AssertionError("nested child must resolve through the claimed API")


async def api_plain_async_nested_root(value: BinaryInput) -> BinaryOutput:
    return await api_plain_async_child(value)


@node
async def api_task_id_keyword_child(task_id: BinaryInput) -> BinaryOutput:
    raise AssertionError("nested child must resolve through the claimed API")


async def api_task_id_keyword_nested_root(value: BinaryInput) -> BinaryOutput:
    return await api_task_id_keyword_child(task_id=value)


@node
async def api_node_runner_keyword_child(node_runner: BinaryInput) -> BinaryOutput:
    raise AssertionError("nested child must resolve through the claimed API")


async def api_node_runner_keyword_nested_root(value: BinaryInput) -> BinaryOutput:
    return await api_node_runner_keyword_child(node_runner=value)


@node
async def api_child_node(value: BinaryInput, **_kwargs) -> BinaryOutput:
    raise AssertionError("nested child must execute on its claimed runner")


async def api_nested_root_node(value: BinaryInput, **kwargs) -> BinaryOutput:
    return await api_child_node(value, **kwargs)


@node
def api_sync_child_node(value: BinaryInput, **_kwargs) -> BinaryOutput:
    return BinaryOutput(payload=value.payload)


async def api_async_root_with_sync_child(value: BinaryInput, **kwargs) -> BinaryOutput:
    return api_sync_child_node(value, **kwargs)


async def api_legacy_db_node(value: BinaryInput, **_kwargs) -> BinaryOutput:
    from simstack.core.context import context as legacy_context

    _ = legacy_context.repository
    return BinaryOutput(payload=value.payload)


class _ForbiddenRunnerDatabaseContext:
    @property
    def db(self):
        raise RuntimeError(
            "Direct database access is forbidden in API-only runner mode"
        )

    @property
    def repository(self):
        raise RuntimeError(
            "Direct database access is forbidden in API-only runner mode"
        )


class _MappingTable:
    def __init__(self, mappings):
        self._by_name = {mapping.name: mapping for mapping in mappings}
        self._by_mapping = {
            getattr(
                mapping, "mapping", getattr(mapping, "function_mapping", None)
            ): mapping
            for mapping in mappings
        }

    def get_by_name(self, name):
        return self._by_name.get(name)

    def get_by_mapping(self, mapping):
        return self._by_mapping.get(mapping)

    def all_mappings(self):
        return tuple(self._by_mapping.values())


def _model_mapping(model_type):
    return SimpleNamespace(
        name=model_type.__name__,
        mapping=f"{__name__}.{model_type.__name__}",
    )


def _node_mapping(function):
    return SimpleNamespace(
        name=function.__name__,
        function_mapping=f"{__name__}.{function.__name__}",
        input_mappings=[
            SimpleNamespace(name="value", mapping=f"{__name__}.BinaryInput")
        ],
        result_mappings=[
            SimpleNamespace(name="result", mapping=f"{__name__}.BinaryOutput")
        ],
        version="1",
        category=None,
        description=None,
    )


def _wire_document(model):
    document = model.model_dump_doc()
    document["id"] = str(document.pop("_id"))
    return document


def _api_context(tmp_path, *, node_functions, **api_methods):
    model_mappings = [
        _model_mapping(BinaryInput),
        _model_mapping(BinaryOutput),
        SimpleNamespace(
            name="BooleanData",
            mapping="simstack.models.base_types.BooleanData",
        ),
        SimpleNamespace(
            name="FileStack",
            mapping="simstack.models.files.FileStack",
        ),
        SimpleNamespace(
            name="FileListModel",
            mapping="simstack.models.file_list.FileListModel",
        ),
    ]
    methods = dict(
        resolve_task_cache=AsyncMock(return_value=None),
        update_task_status=AsyncMock(return_value={}),
        commit_task=AsyncMock(return_value={}),
        fail_task=AsyncMock(return_value={}),
        renew_lease=AsyncMock(return_value={}),
    )
    methods.update(api_methods)
    runner_api = SimpleNamespace(**methods)
    return SimpleNamespace(
        runner_mode=True,
        runner_api=runner_api,
        model_mappings=_MappingTable(model_mappings),
        node_mappings=_MappingTable(
            [_node_mapping(function) for function in node_functions]
        ),
        config=SimpleNamespace(workdir=tmp_path),
    )


def _task(function, input_model):
    registry = NodeRegistry(
        name=function.__name__,
        status=TaskStatus.RETRIEVED,
        function_hash=node_code_version(getattr(function, "_inner", function)),
        arg_hash=compute_arg_hash([input_model]),
        func_mapping=f"{__name__}.{function.__name__}",
        parameters=Parameters(),
        call_path=f".{function.__name__}",
    )
    registry_payload = registry.model_dump(mode="json")
    task = {field: registry_payload.get(field) for field in RUNNER_TASK_WIRE_FIELDS}
    task["task_schema_version"] = 1
    task["created_at"] = "2026-08-17T12:00:00Z"
    task["inputs"] = [
        {
            "variable_name": "value",
            "variable_mapping": f"{__name__}.BinaryInput",
            "reference": str(input_model.id),
            "value": _wire_document(input_model),
            "dependencies": [],
        }
    ]
    task["results"] = []
    task["info_files"] = {"elements": []}
    task["artifact_plan"] = {
        "required": False,
        "mappings": [],
        "child_artifacts": [],
    }
    return task, registry


def test_runtime_model_resolution_never_imports_unrelated_bootstrap_mappings(
    monkeypatch,
    tmp_path,
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    expected = _model_mapping(BinaryInput)
    fake_context.model_mappings = _MappingTable(
        [
            SimpleNamespace(
                name="UnrelatedExampleModel",
                mapping="examples.unavailable.workflow.UnrelatedExampleModel",
            ),
            expected,
        ]
    )
    _patch_runner_context(monkeypatch, fake_context)
    import_module = runner_mappings.importlib.import_module

    def reject_unrelated_import(module_name):
        if module_name.startswith("examples.unavailable"):
            raise AssertionError("unrelated bootstrap mapping was imported")
        return import_module(module_name)

    monkeypatch.setattr(
        runner_mappings.importlib,
        "import_module",
        reject_unrelated_import,
    )

    assert runner_mappings._mapping_for_model(BinaryInput) is expected


def test_runtime_model_resolution_rejects_noncanonical_same_name_reexport(
    monkeypatch,
    tmp_path,
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    alias = SimpleNamespace(
        name="BinaryInput",
        mapping=f"{__name__}.BinaryInputAlias",
    )
    fake_context.model_mappings = _MappingTable([alias])
    _patch_runner_context(monkeypatch, fake_context)

    with pytest.raises(
        RunnerTaskPayloadError,
        match="does not have a canonical bootstrap mapping",
    ):
        runner_mappings._mapping_for_model(BinaryInput)


def test_hydrate_inputs_restores_binary_and_object_list_dependencies(
    monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    file_stack = FileStack(
        name="payload.bin",
        size=3,
        in_memory=True,
        content=b"\x00\x01\xff",
    )
    file_list = FileListModel(elements=[file_stack])
    task = {
        "inputs": [
            {
                "variable_name": "files",
                "variable_mapping": "simstack.models.file_list.FileListModel",
                "reference": str(file_list.id),
                "value": _wire_document(file_list),
                "dependencies": [
                    {
                        "variable_mapping": "simstack.models.files.FileStack",
                        "reference": str(file_stack.id),
                        "value": _wire_document(file_stack),
                    }
                ],
            }
        ]
    }

    (hydrated_list,) = hydrate_task_inputs(task)

    assert isinstance(hydrated_list, FileListModel)
    assert list(hydrated_list)[0].content == b"\x00\x01\xff"


@pytest.mark.parametrize(
    "mutation",
    ("missing_reference", "missing_value_id", "null_dependencies", "extra_field"),
)
def test_hydrate_inputs_rejects_noncanonical_nested_v1_documents(
    mutation, monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    input_model = BinaryInput(payload=b"input")
    entry = {
        "variable_name": "value",
        "variable_mapping": f"{__name__}.BinaryInput",
        "reference": str(input_model.id),
        "value": _wire_document(input_model),
        "dependencies": [],
    }
    if mutation == "missing_reference":
        entry.pop("reference")
    elif mutation == "missing_value_id":
        entry["value"].pop("id")
    elif mutation == "null_dependencies":
        entry["dependencies"] = None
    else:
        entry["legacy_extra"] = {}

    with pytest.raises(RunnerTaskPayloadError):
        hydrate_task_inputs({"inputs": [entry]})


def test_runner_hydration_rejects_idless_file_instance(monkeypatch, tmp_path):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    location = FileInstance(
        path="remote/task/output.dat",
        resource=Resource(value="remote"),
        created_at=datetime(2026, 8, 17, 12, 0, 0),
    )
    file_stack = FileStack(
        name="output.dat",
        in_memory=False,
        locations=[location],
    )
    task = {
        "inputs": [
            {
                "variable_name": "files",
                "variable_mapping": "simstack.models.files.FileStack",
                "reference": str(file_stack.id),
                "value": _wire_document(file_stack),
                "dependencies": [],
            }
        ]
    }

    with pytest.raises(RunnerTaskPayloadError, match="FileInstance"):
        hydrate_task_inputs(task)


def test_serialize_result_uses_lossless_binary_wire_shape(monkeypatch, tmp_path):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)

    payload = serialize_runner_result(
        BinaryOutput(payload=b"\x00\xff"),
        result_mappings=[
            SimpleNamespace(name="BinaryOutput", mapping=f"{__name__}.BinaryOutput")
        ],
    )

    assert payload.results == [
        {
            "variable_name": "BinaryOutput",
            "variable_mapping": f"{__name__}.BinaryOutput",
            "value": {
                "id": payload.results[0]["value"]["id"],
                "payload": {"$binary": {"base64": "AP8=", "subType": "00"}},
            },
            "dependencies": [],
        }
    ]


def test_serialize_result_includes_write_side_dependency_closure(monkeypatch, tmp_path):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    file_stack = FileStack(
        name="payload.bin",
        size=3,
        in_memory=True,
        content=b"\x00\x01\xff",
    )

    payload = serialize_runner_result(
        FileListModel(elements=[file_stack]),
        result_mappings=[
            SimpleNamespace(
                name="files", mapping="simstack.models.file_list.FileListModel"
            )
        ],
    )

    assert payload.results[0]["dependencies"] == [
        {
            "variable_mapping": "simstack.models.files.FileStack",
            "reference": str(file_stack.id),
            "value": _wire_document(file_stack),
        }
    ]


def test_serialize_success_boolean_preserves_legacy_boolean_result(
    monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)

    payload = serialize_runner_result(
        True,
        result_mappings=[
            SimpleNamespace(
                name="value", mapping="simstack.models.base_types.BooleanData"
            )
        ],
    )

    assert payload.results[0]["variable_name"] == "value"
    assert payload.results[0]["variable_mapping"].endswith(".BooleanData")
    assert payload.results[0]["value"]["value"] is True


def test_serialize_result_requires_every_declared_output_pair(monkeypatch, tmp_path):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    declared = [
        SimpleNamespace(name="BinaryOutput", mapping=f"{__name__}.BinaryOutput"),
        SimpleNamespace(name="secondary", mapping=f"{__name__}.BinaryOutput"),
    ]

    with pytest.raises(RunnerTaskExecutionError, match="omitted declared result"):
        serialize_runner_result(
            BinaryOutput(payload=b"one"),
            result_mappings=declared,
        )


@pytest.mark.asyncio
async def test_execute_claimed_root_is_api_only(monkeypatch, tmp_path):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    task, registry = _task(api_root_node, BinaryInput(payload=b"input"))

    succeeded = await execute_claimed_task(task, claim_id="claim-1", attempt=2)

    assert succeeded is True
    status_update = fake_context.runner_api.update_task_status.await_args
    assert status_update.args == (str(registry.id),)
    assert status_update.kwargs["claim_id"] == "claim-1"
    assert status_update.kwargs["attempt"] == 2
    assert status_update.kwargs["expected_status"] == "retrieved"
    assert status_update.kwargs["status"] == "running"
    assert status_update.kwargs["observed_at"].endswith("+00:00")
    commit = fake_context.runner_api.commit_task.await_args
    assert commit.args == (str(registry.id),)
    assert commit.kwargs["claim_id"] == "claim-1"
    assert commit.kwargs["attempt"] == 2
    assert commit.kwargs["expected_status"] == "running"
    assert commit.kwargs["results"][0]["variable_name"] == "result"
    assert commit.kwargs["results"][0]["value"]["payload"] == encode_document_value(
        b"input-done"
    )
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_output_publication_failure_prevents_terminal_commit(monkeypatch, tmp_path):
    from simstack.runner_api.file_transfer import FileTransferError

    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    task, registry = _task(api_root_node, BinaryInput(payload=b"input"))

    def unavailable(roots):
        assert roots[0].payload == b"input-done"
        raise FileTransferError("Durable output storage is unavailable")

    monkeypatch.setattr("simstack.core.runner_execution.blobs.publish_runtime_blobs", unavailable)
    succeeded = await execute_claimed_task(task, claim_id="claim-storage", attempt=2)
    assert succeeded is False
    fake_context.runner_api.commit_task.assert_not_awaited()
    failure = fake_context.runner_api.fail_task.await_args
    assert failure.args == (str(registry.id),)
    assert "Durable output storage is unavailable" in failure.kwargs["message"]


@pytest.mark.asyncio
async def test_ordinary_increment_signature_executes_without_metadata_kwargs(
    monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_increment_node])
    _patch_runner_context(monkeypatch, fake_context)
    task, _ = _task(api_increment_node, BinaryInput(payload=b"41"))

    assert await execute_claimed_task(task, claim_id="plain-increment", attempt=1)

    commit = fake_context.runner_api.commit_task.await_args.kwargs
    assert commit["results"][0]["value"]["payload"] == encode_document_value(b"42")
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.parametrize(
    ("function", "input_name"),
    [
        (api_task_id_model_input, "task_id"),
        (api_node_runner_positional_input, "node_runner"),
        (api_task_id_input_with_metadata, "task_id"),
        (api_node_runner_positional_with_metadata, "node_runner"),
    ],
)
@pytest.mark.asyncio
async def test_model_inputs_take_precedence_over_same_named_execution_metadata(
    monkeypatch, tmp_path, function, input_name
):
    fake_context = _api_context(tmp_path, node_functions=[function])
    mapping = fake_context.node_mappings.get_by_name(function.__name__)
    mapping.input_mappings[0].name = input_name
    _patch_runner_context(monkeypatch, fake_context)
    task, _ = _task(function, BinaryInput(payload=b"model-input"))
    task["inputs"][0]["variable_name"] = input_name

    assert await execute_claimed_task(task, claim_id="model-input", attempt=1)

    commit = fake_context.runner_api.commit_task.await_args.kwargs
    assert commit["results"][0]["value"]["payload"] == encode_document_value(
        b"model-input"
    )
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.parametrize(
    "input_name",
    [
        "node_runner",
        "parent_id",
        "task_id",
        "call_path",
        "parent_parameters",
        "recompute_artifacts",
        "custom_name",
        "arg_hash",
        "function_hash",
        "force_rerun",
    ],
)
@pytest.mark.asyncio
async def test_variadic_keyword_model_inputs_do_not_get_overwritten_by_metadata(
    monkeypatch, tmp_path, input_name
):
    function = api_metadata_named_input_via_kwargs
    fake_context = _api_context(tmp_path, node_functions=[function])
    fake_context.node_mappings.get_by_name(function.__name__).input_mappings[
        0
    ].name = input_name
    _patch_runner_context(monkeypatch, fake_context)
    task, _ = _task(function, BinaryInput(payload=b"variadic-input"))
    task["inputs"][0]["variable_name"] = input_name
    task["parameters"]["force_rerun"] = True

    assert await execute_claimed_task(task, claim_id="variadic-input", attempt=1)

    commit = fake_context.runner_api.commit_task.await_args.kwargs
    assert commit["results"][0]["value"]["payload"] == encode_document_value(
        b"variadic-input"
    )
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_async_root_and_child_use_claim_api_without_kwargs(
    monkeypatch, tmp_path
):
    child_result = BinaryOutput(payload=b"plain-async-child")
    resolve_child = AsyncMock(
        return_value=ResolvedChildTask(
            request_id="plain-child",
            cache_hit=True,
            child_id="507f1f77bcf86cd799439012",
            status="completed",
            task={
                "return_kind": "model",
                "results": [
                    {
                        "variable_name": "result",
                        "variable_mapping": f"{__name__}.BinaryOutput",
                        "reference": str(child_result.id),
                        "value": _wire_document(child_result),
                        "dependencies": [],
                    }
                ],
            },
        )
    )
    fake_context = _api_context(
        tmp_path,
        node_functions=[api_plain_async_nested_root, api_plain_async_child],
        resolve_child=resolve_child,
    )
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    task, registry = _task(api_plain_async_nested_root, BinaryInput(payload=b"input"))

    assert await execute_claimed_task(task, claim_id="plain-async", attempt=2)

    resolve_child.assert_awaited_once()
    request = resolve_child.await_args
    assert request.args == (str(registry.id),)
    assert request.kwargs["claim_id"] == "plain-async"
    assert request.kwargs["attempt"] == 2
    assert request.kwargs["node"]["call_path"] == (
        ".api_plain_async_nested_root.api_plain_async_child"
    )
    assert request.kwargs["inputs"][0]["variable_name"] == "value"
    commit = fake_context.runner_api.commit_task.await_args.kwargs
    assert commit["results"][0]["value"]["payload"] == encode_document_value(
        b"plain-async-child"
    )
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.parametrize(
    ("root", "child", "input_name"),
    [
        (api_task_id_keyword_nested_root, api_task_id_keyword_child, "task_id"),
        (
            api_node_runner_keyword_nested_root,
            api_node_runner_keyword_child,
            "node_runner",
        ),
    ],
)
@pytest.mark.asyncio
async def test_nested_keyword_models_are_bound_by_signature_not_reserved_names(
    monkeypatch, tmp_path, root, child, input_name
):
    child_result = BinaryOutput(payload=b"keyword-child")
    resolve_child = AsyncMock(
        return_value=ResolvedChildTask(
            request_id="keyword-child",
            cache_hit=True,
            child_id="507f1f77bcf86cd799439012",
            status="completed",
            task={
                "return_kind": "model",
                "results": [
                    {
                        "variable_name": "result",
                        "variable_mapping": f"{__name__}.BinaryOutput",
                        "reference": str(child_result.id),
                        "value": _wire_document(child_result),
                        "dependencies": [],
                    }
                ],
            },
        )
    )
    fake_context = _api_context(
        tmp_path, node_functions=[root, child], resolve_child=resolve_child
    )
    fake_context.node_mappings.get_by_name(child.__name__).input_mappings[
        0
    ].name = input_name
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    task, registry = _task(root, BinaryInput(payload=b"keyword-input"))

    assert await execute_claimed_task(task, claim_id="keyword-model", attempt=2)

    resolve_child.assert_awaited_once()
    request = resolve_child.await_args
    assert request.args == (str(registry.id),)
    assert request.kwargs["claim_id"] == "keyword-model"
    assert request.kwargs["attempt"] == 2
    assert request.kwargs["node"]["call_path"] == (f".{root.__name__}.{child.__name__}")
    assert request.kwargs["inputs"][0]["variable_name"] == input_name
    assert request.kwargs["inputs"][0]["value"]["payload"] == encode_document_value(
        b"keyword-input"
    )
    fake_context.runner_api.fail_task.assert_not_awaited()
    assert fake_context.runner_api.commit_task.await_args.kwargs["results"][0]["value"][
        "payload"
    ] == encode_document_value(b"keyword-child")


@pytest.mark.asyncio
async def test_lease_is_renewed_during_cache_resolution_and_artifact_commit(
    monkeypatch, tmp_path
):
    stage = "initial"
    renewed_stages = []

    async def cache(*args, **kwargs):
        nonlocal stage
        stage = "cache"
        await asyncio.sleep(0.015)
        return None

    async def commit(*args, **kwargs):
        nonlocal stage
        stage = "commit"
        await asyncio.sleep(0.015)

    async def renew(*args, **kwargs):
        renewed_stages.append(stage)

    fake_context = _api_context(
        tmp_path,
        node_functions=[api_root_node],
        resolve_task_cache=AsyncMock(side_effect=cache),
        commit_task=AsyncMock(side_effect=commit),
        renew_lease=AsyncMock(side_effect=renew),
    )
    _patch_runner_context(monkeypatch, fake_context)

    async def fast_lease(claim, *, lease_seconds, stop):
        while not stop.is_set():
            await asyncio.sleep(0.001)
            if not stop.is_set():
                await fake_context.runner_api.renew_lease(
                    claim.task_id,
                    claim_id=claim.claim_id,
                    attempt=claim.attempt,
                    lease_seconds=lease_seconds,
                )

    monkeypatch.setattr(runner_lifecycle, "_renew_lease_until_stopped", fast_lease)
    task, _ = _task(api_root_node, BinaryInput(payload=b"input"))
    assert await execute_claimed_task(task, claim_id="long-commit", attempt=1)
    assert {"cache", "commit"} <= set(renewed_stages)
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_failure_cancels_cache_resolution_before_body_or_commit(
    monkeypatch, tmp_path
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def cache(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    fake_context = _api_context(
        tmp_path,
        node_functions=[api_root_node],
        resolve_task_cache=AsyncMock(side_effect=cache),
    )
    _patch_runner_context(monkeypatch, fake_context)

    async def lost_lease(claim, *, lease_seconds, stop):
        await entered.wait()
        raise RunnerTaskExecutionError("claim lease lost")

    monkeypatch.setattr(runner_lifecycle, "_renew_lease_until_stopped", lost_lease)
    task, _ = _task(api_root_node, BinaryInput(payload=b"input"))
    assert not await execute_claimed_task(task, claim_id="lost-cache", attempt=1)
    assert cancelled.is_set()
    fake_context.runner_api.commit_task.assert_not_awaited()
    fake_context.runner_api.fail_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_commit_response_wins_lease_revocation_race(
    monkeypatch, tmp_path
):
    entered = asyncio.Event()

    async def commit(*args, **kwargs):
        entered.set()
        await asyncio.sleep(0.005)

    fake_context = _api_context(
        tmp_path,
        node_functions=[api_root_node],
        commit_task=AsyncMock(side_effect=commit),
    )
    _patch_runner_context(monkeypatch, fake_context)

    async def terminal_lease(claim, *, lease_seconds, stop):
        await entered.wait()
        raise RunnerTaskExecutionError("already completed claim")

    monkeypatch.setattr(runner_lifecycle, "_renew_lease_until_stopped", terminal_lease)
    task, _ = _task(api_root_node, BinaryInput(payload=b"input"))
    assert await execute_claimed_task(task, claim_id="terminal-race", attempt=1)
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_logs_emit_and_flush_before_success_commit(monkeypatch, tmp_path):
    order: list[str] = []

    async def update_status(*_args, **_kwargs):
        order.append("status")
        return {}

    async def commit(*_args, **_kwargs):
        order.append("commit")
        return {}

    fake_context = _api_context(
        tmp_path,
        node_functions=[api_root_node],
        update_task_status=AsyncMock(side_effect=update_status),
        commit_task=AsyncMock(side_effect=commit),
    )
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr(
        runner_lifecycle,
        "flush_runner_api_logs",
        lambda: order.append("flush"),
    )
    monkeypatch.setattr(
        runner_lifecycle.logger,
        "info",
        lambda *_args, **_kwargs: order.append("success-log"),
    )
    task, _ = _task(api_root_node, BinaryInput(payload=b"input"))

    assert await execute_claimed_task(task, claim_id="claim-log", attempt=1)
    assert order == ["status", "success-log", "flush", "commit"]


@pytest.mark.asyncio
async def test_api_logs_flush_before_failure_mutation(monkeypatch, tmp_path):
    order: list[str] = []

    async def fail_task(*_args, **_kwargs):
        order.append("fail")
        return {}

    fake_context = _api_context(
        tmp_path,
        node_functions=[api_legacy_db_node],
        fail_task=AsyncMock(side_effect=fail_task),
    )
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr(
        "simstack.core.context.context", _ForbiddenRunnerDatabaseContext()
    )
    monkeypatch.setattr(
        runner_lifecycle,
        "flush_runner_api_logs",
        lambda: order.append("flush"),
    )
    monkeypatch.setattr(
        runner_lifecycle.logger,
        "exception",
        lambda *_args, **_kwargs: order.append("failure-log"),
    )
    task, _ = _task(api_legacy_db_node, BinaryInput(payload=b"input"))

    assert not await execute_claimed_task(task, claim_id="claim-log-fail", attempt=1)
    assert order[-3:] == ["failure-log", "flush", "fail"]


@pytest.mark.asyncio
async def test_execute_claimed_root_requires_every_declared_input_pair(
    monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    task, registry = _task(api_root_node, BinaryInput(payload=b"input"))
    task["inputs"] = []

    succeeded = await execute_claimed_task(
        task,
        claim_id="claim-missing-input",
        attempt=3,
    )

    assert succeeded is False
    fake_context.runner_api.update_task_status.assert_not_awaited()
    fake_context.runner_api.commit_task.assert_not_awaited()
    failure = fake_context.runner_api.fail_task.await_args
    assert failure.args == (str(registry.id),)
    assert "omitted declared inputs" in failure.kwargs["message"]


@pytest.mark.asyncio
async def test_user_node_legacy_database_access_fails_through_api(
    monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_legacy_db_node])
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr(
        "simstack.core.context.context", _ForbiddenRunnerDatabaseContext()
    )
    task, registry = _task(api_legacy_db_node, BinaryInput(payload=b"input"))

    succeeded = await execute_claimed_task(
        task,
        claim_id="claim-legacy-db",
        attempt=4,
    )

    assert succeeded is False
    fake_context.runner_api.commit_task.assert_not_awaited()
    failure = fake_context.runner_api.fail_task.await_args
    assert failure.args == (str(registry.id),)
    assert failure.kwargs["attempt"] == 4
    assert failure.kwargs["error"] == "RuntimeError"
    assert "Direct database access is forbidden" in failure.kwargs["message"]


@pytest.mark.asyncio
async def test_artifact_requests_are_committed_for_server_owned_rendering(
    monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    task, registry = _task(api_root_node, BinaryInput(payload=b"input"))
    task["parameters"]["recompute_artifacts"] = True

    succeeded = await execute_claimed_task(task, claim_id="claim-artifacts", attempt=3)

    assert succeeded is True
    commit = fake_context.runner_api.commit_task.await_args
    assert commit.args == (str(registry.id),)
    assert commit.kwargs["claim_id"] == "claim-artifacts"
    assert commit.kwargs["attempt"] == 3
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_artifact_plan_fails_closed(monkeypatch, tmp_path):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    task, _ = _task(api_root_node, BinaryInput(payload=b"input"))
    task.pop("artifact_plan")

    with pytest.raises(RunnerTaskPayloadError, match="(?s)artifact_plan.*required"):
        node_registry_from_task(task)

    fake_context.runner_api.commit_task.assert_not_awaited()
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_execution_rejects_task_dto_protocol_drift(mutation):
    task, _ = _task(api_root_node, BinaryInput(payload=b"input"))
    if mutation == "extra":
        task["legacy_node"] = {}
        expected = "(?s)legacy_node.*Extra"
    else:
        task.pop("inputs")
        expected = "(?s)inputs.*required"

    with pytest.raises(RunnerTaskPayloadError, match=expected):
        node_registry_from_task(task)


@pytest.mark.asyncio
async def test_artifact_plan_rejects_extra_fields(monkeypatch, tmp_path):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    task, _ = _task(api_root_node, BinaryInput(payload=b"input"))
    task["artifact_plan"]["unscoped"] = []

    with pytest.raises(RunnerTaskPayloadError, match="(?s)unscoped.*Extra"):
        await execute_claimed_task(
            task,
            claim_id="claim-extra-plan",
            attempt=9,
        )

    fake_context.runner_api.commit_task.assert_not_awaited()
    fake_context.runner_api.fail_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_decorated_node_resolves_through_parent_claim(
    monkeypatch, tmp_path
):
    nested_result = BinaryOutput(payload=b"nested-result")
    resolve_child = AsyncMock(
        return_value=ResolvedChildTask(
            request_id="request-1",
            cache_hit=True,
            child_id="507f1f77bcf86cd799439012",
            status="completed",
            task={
                "results": [
                    {
                        "variable_name": "BinaryOutput",
                        "variable_mapping": f"{__name__}.BinaryOutput",
                        "reference": str(nested_result.id),
                        "value": _wire_document(nested_result),
                        "dependencies": [],
                    }
                ]
            },
        )
    )
    fake_context = _api_context(
        tmp_path,
        node_functions=[api_nested_root_node, api_child_node],
        resolve_child=resolve_child,
        get_child=AsyncMock(),
    )
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    task, _ = _task(api_nested_root_node, BinaryInput(payload=b"input"))

    succeeded = await execute_claimed_task(task, claim_id="claim-2", attempt=4)

    assert succeeded is True
    resolve_child.assert_awaited_once()
    request = resolve_child.await_args.kwargs
    assert request["claim_id"] == "claim-2"
    assert request["attempt"] == 4
    assert request["node"]["function_mapping"].endswith(".api_child_node")
    assert request["inputs"][0]["variable_name"] == "value"
    assert request["inputs"][0]["value"]["payload"] == encode_document_value(b"input")
    committed = fake_context.runner_api.commit_task.await_args.kwargs["results"]
    assert committed[0]["value"]["payload"] == encode_document_value(b"nested-result")


@pytest.mark.asyncio
async def test_sync_decorated_child_from_async_root_uses_api_on_reentrant_loop(
    monkeypatch, tmp_path
):
    fake_context = _api_context(
        tmp_path,
        node_functions=[api_async_root_with_sync_child, api_sync_child_node],
        resolve_child=AsyncMock(
            return_value=ResolvedChildTask(
                request_id="sync-child",
                cache_hit=True,
                child_id="child-one",
                status="completed",
                task={
                    "return_kind": "model",
                    "results": [
                        {
                            "variable_name": "BinaryOutput",
                            "variable_mapping": f"{__name__}.BinaryOutput",
                            "reference": "507f1f77bcf86cd799439012",
                            "value": {
                                "id": "507f1f77bcf86cd799439012",
                                "payload": encode_document_value(b"sync-child"),
                            },
                            "dependencies": [],
                        }
                    ],
                },
            )
        ),
    )
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    task, _ = _task(
        api_async_root_with_sync_child,
        BinaryInput(payload=b"input"),
    )

    succeeded = await execute_claimed_task(
        task,
        claim_id="claim-sync-child",
        attempt=1,
    )

    assert succeeded is True
    fake_context.runner_api.resolve_child.assert_awaited_once()
    fake_context.runner_api.fail_task.assert_not_awaited()
    committed = fake_context.runner_api.commit_task.await_args.kwargs
    assert committed["results"][0]["value"]["payload"] == encode_document_value(
        b"sync-child"
    )


@pytest.mark.asyncio
async def test_ui_sentinel_identity_is_established_through_claim_scoped_api(
    monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    value = BinaryInput(payload=b"ui-input")
    task, _ = _task(api_root_node, value)
    task["function_hash"] = task["arg_hash"] = "NOT INITIALIZED"
    assert await execute_claimed_task(task, claim_id="ui-claim", attempt=1)
    identity = fake_context.runner_api.update_task_status.await_args.kwargs
    assert identity["function_hash"] == node_code_version(api_root_node)
    assert identity["arg_hash"] == compute_arg_hash([value])
    assert identity["is_async"] is True
    assert identity["claim_id"] == "ui-claim"


@pytest.mark.asyncio
async def test_established_code_identity_mismatch_fails_before_execution(
    monkeypatch, tmp_path
):
    fake_context = _api_context(tmp_path, node_functions=[api_root_node])
    _patch_runner_context(monkeypatch, fake_context)
    task, _ = _task(api_root_node, BinaryInput(payload=b"input"))
    task["function_hash"] = "different-implementation"
    assert not await execute_claimed_task(task, claim_id="claim-mismatch", attempt=1)
    fake_context.runner_api.update_task_status.assert_not_awaited()
    fake_context.runner_api.commit_task.assert_not_awaited()
    assert (
        "code version mismatch"
        in fake_context.runner_api.fail_task.await_args.kwargs["message"]
    )


def test_boolean_child_return_kind_survives_api_hydration(monkeypatch, tmp_path):
    from simstack.models.base_types import BooleanData

    fake_context = _api_context(tmp_path, node_functions=[])
    _patch_runner_context(monkeypatch, fake_context)
    result = BooleanData(value=True)
    assert (
        hydrate_task_results(
            {
                "return_kind": "bool",
                "results": [
                    {
                        "variable_name": "value",
                        "variable_mapping": "simstack.models.base_types.BooleanData",
                        "reference": str(result.id),
                        "value": _wire_document(result),
                        "dependencies": [],
                    }
                ],
            }
        )
        is True
    )


def test_empty_multiple_child_preserves_metadata_and_info_files(monkeypatch, tmp_path):
    from simstack.core.simstack_result import SimstackResult

    _patch_runner_context(monkeypatch, _api_context(tmp_path, node_functions=[]))
    info_file = FileStack(name="information.txt", in_memory=True, content=b"info")
    result = hydrate_task_results(
        {
            "return_kind": "multiple",
            "results": [],
            "custom_name": "metadata-only",
            "message": "finished",
            "error": "warning",
            "info_files": {
                "elements": [
                    {
                        "variable_mapping": "simstack.models.files.FileStack",
                        "reference": str(info_file.id),
                        "value": _wire_document(info_file),
                        "dependencies": [],
                    }
                ]
            },
        }
    )
    assert isinstance(result, SimstackResult)
    assert result.custom_name == "metadata-only"
    assert result.message == "finished"
    assert result.error_message == "warning"
    assert result.info_files == [info_file]


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter_source", ["decorator", "call"])
async def test_explicit_self_parameters_inherit_unset_parent_execution_route(
    monkeypatch,
    tmp_path,
    parameter_source,
):
    from simstack.core.runner_execution.state import (
        RunnerClaimContext,
        activate_runner_claim,
        reset_runner_claim,
    )

    @node(
        parameters=Parameters(resource="self")
        if parameter_source == "decorator"
        else None
    )
    async def inherited_child(value: BinaryInput, **kwargs) -> BinaryOutput:
        raise AssertionError("child must resolve through API")

    resolve = AsyncMock(
        return_value=ResolvedChildTask(
            request_id="inherit",
            cache_hit=True,
            child_id="inherited-child",
            status="completed",
            task={"return_kind": "multiple", "results": []},
        )
    )
    fake_context = _api_context(
        tmp_path, node_functions=[inherited_child], resolve_child=resolve
    )
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    parent = Parameters(
        queue="slurm-queue", in_docker=True, slurm_parameters={"nodes": 2}
    )
    kwargs = {"parent_parameters": parent}
    if parameter_source == "call":
        kwargs["parameters"] = Parameters(resource="self")
    token = activate_runner_claim(RunnerClaimContext("parent", "claim", 1))
    try:
        await inherited_child(BinaryInput(payload=b"input"), **kwargs)
    finally:
        reset_runner_claim(token)
    parameters = resolve.await_args.kwargs["node"]["parameters"]
    assert parameters["queue"] == "slurm-queue"
    assert parameters["in_docker"] is True
    assert parameters["slurm_parameters"]["nodes"] == 2


def test_parameter_provenance_distinguishes_omitted_and_explicit_defaults():
    assert Parameters(resource="self").model_fields_set == {"resource"}
    assert "queue" in Parameters(resource="self", queue="default").model_fields_set
    assert "in_docker" in Parameters(resource="self", in_docker=False).model_fields_set


@pytest.mark.asyncio
async def test_repeated_nested_invocations_have_distinct_idempotency_ids_same_cache_identity(
    monkeypatch, tmp_path
):
    from simstack.core.runner_execution.state import (
        RunnerClaimContext,
        activate_runner_claim,
        reset_runner_claim,
    )

    resolve = AsyncMock(
        return_value=ResolvedChildTask(
            request_id="logical-invocation",
            cache_hit=True,
            child_id="same-cached-child",
            status="completed",
            task={"return_kind": "multiple", "results": []},
        )
    )
    fake_context = _api_context(
        tmp_path, node_functions=[api_child_node], resolve_child=resolve
    )
    _patch_runner_context(monkeypatch, fake_context)
    monkeypatch.setattr("simstack.core.node.context", fake_context)
    value = BinaryInput(payload=b"same-input")
    token = activate_runner_claim(RunnerClaimContext("parent", "claim", 1))
    try:
        await api_child_node(value, custom_name="first invocation")
        await api_child_node(value, custom_name="second invocation")
    finally:
        reset_runner_claim(token)
    first, second = [call.kwargs for call in resolve.await_args_list]
    assert first["request_id"] != second["request_id"]
    assert len(first["request_id"]) == len(second["request_id"]) == 32
    assert first["node"]["function_hash"] == second["node"]["function_hash"]
    assert first["node"]["arg_hash"] == second["node"]["arg_hash"]
    assert first["node"]["custom_name"] != second["node"]["custom_name"]
