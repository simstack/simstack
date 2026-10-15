from __future__ import annotations

import builtins
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

import simstack.core.context as context_module
import simstack.core.run_node as run_node_module
import simstack.core.runner_execution.lifecycle as task_execution_module
import simstack.core.runner_execution.mappings as task_mapping_module
import simstack.core.runner_execution.nested as nested_execution_module
from simstack.core.context import GlobalState
from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.models.document import Model, ObjectId
from simstack.core.node import compute_arg_hash
from simstack.core.node_code_version import node_code_version
from simstack.models.models import DataMapping, ModelMapping, NodeModel
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters
from simstack.runner_api import SupervisorRunnerClient
from simstack.runner_protocol import (
    RUNNER_SUPERVISOR_SCOPES,
    RUNNER_TASK_WIRE_FIELDS,
)


class HttpInput(Model):
    payload: str


class HttpOutput(Model):
    payload: str


async def http_success_node(value: HttpInput, **_kwargs) -> HttpOutput:
    return HttpOutput(payload=f"{value.payload}-done")


async def http_legacy_db_node(value: HttpInput, **_kwargs) -> HttpOutput:
    from simstack.core.context import context

    _ = context.repository
    return HttpOutput(payload=value.payload)


def _mapping_path(value: object) -> str:
    return f"{__name__}.{getattr(value, '__name__')}"


def _wire_document(model: Model) -> dict[str, object]:
    document = model.model_dump_doc()
    document["id"] = str(document.pop("_id"))
    return document


def _task_bundle(function, task_id: str) -> dict[str, object]:
    input_value = HttpInput(payload="http")
    registry = NodeRegistry(
        id=ObjectId(task_id),
        name=function.__name__,
        status=TaskStatus.RETRIEVED,
        function_hash=node_code_version(function),
        arg_hash=compute_arg_hash([input_value]),
        func_mapping=_mapping_path(function),
        parameters=Parameters(),
        call_path=f".{function.__name__}",
    )
    registry_payload = registry.model_dump(mode="json")
    task = {field: registry_payload.get(field) for field in RUNNER_TASK_WIRE_FIELDS}
    task.update(
        {
            "task_schema_version": 1,
            "id": task_id,
            "created_at": "2026-08-17T12:00:00Z",
            "artifact_plan": {
                "required": False,
                "mappings": [],
                "child_artifacts": [],
            },
            "info_files": {"elements": []},
            "inputs": [
                {
                    "variable_name": "value",
                    "variable_mapping": _mapping_path(HttpInput),
                    "reference": str(input_value.id),
                    "value": _wire_document(input_value),
                    "dependencies": [],
                }
            ],
            "results": [],
        }
    )
    return task


def _bootstrap(function, workdir) -> dict[str, object]:
    model_mappings = [
        ModelMapping(
            name=model.__name__,
            mapping=_mapping_path(model),
            collection_name=model.__collection__,
        ).model_dump(mode="json", exclude={"collection_name"})
        for model in (HttpInput, HttpOutput)
    ]
    node_model = NodeModel(
        name=function.__name__,
        function_mapping=_mapping_path(function),
        input_mappings=[DataMapping(name="value", mapping=_mapping_path(HttpInput))],
        result_mappings=[DataMapping(name="result", mapping=_mapping_path(HttpOutput))],
        called_nodes=[],
        default_parameters=Parameters(),
    ).model_dump(mode="json", exclude={"pickle_function"})
    return {
        "api_version": "1",
        "server_time": "2026-08-17T12:00:00Z",
        "resource": {
            "resource_str": "local",
            "workdir": str(workdir),
            "hostname": "localhost",
            "python_paths": [],
            "environment_start": None,
            "is_default": False,
        },
        "allowed_resources": ["local"],
        "model_mappings": model_mappings,
        "node_models": [node_model],
        "scopes": [
            "tasks:read",
            "tasks:lease",
            "tasks:status",
            "tasks:commit",
            "tasks:fail",
            "tasks:children",
            "logs:write",
            "file:create",
            "file:download",
            "file:status",
            "file:complete",
        ],
        "capabilities": {
            "max_runner_payload_bytes": 1_000_000,
            "max_inline_document_bytes": 500_000,
        },
    }


class _RunnerApiHandler(BaseHTTPRequestHandler):
    server: "_RunnerHttpServer"

    def _respond(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> tuple[str, dict[str, object] | None]:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        path = urlsplit(self.path).path
        self.server.requests.append(
            (self.command, path, self.headers.get("Authorization"), body)
        )
        return path, body

    def do_GET(self) -> None:  # noqa: N802
        path, _ = self._record()
        task_base = f"/api/runner/tasks/{self.server.task_id}"
        if path == f"{task_base}/bootstrap":
            self._respond(self.server.bootstrap_payload)
            return
        if path == task_base:
            self._respond(
                {
                    "claim_id": self.server.claim_id,
                    "attempt": 1,
                    "lease_until": "2026-08-17T12:05:00Z",
                    "execution_token": self.server.execution_token,
                    "execution_token_expires_at": "2026-08-17T12:05:00Z",
                    "task": self.server.task_payload,
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path, body = self._record()
        task_base = f"/api/runner/tasks/{self.server.task_id}"
        if path == f"{task_base}/cache/resolve":
            self._respond({
                "task_id": self.server.task_id, "claim_id": body["claim_id"],
                "attempt": body["attempt"], "cache_hit": False, "task": None,
            })
            return
        if path == f"{task_base}/status":
            self._respond(
                {
                    "task_id": self.server.task_id,
                    "status": body["status"],
                    "accepted": True,
                }
            )
            return
        if path == f"{task_base}/commit":
            self._respond(
                {
                    "task_id": self.server.task_id,
                    "status": "completed",
                    "accepted": True,
                    "result_references": [],
                }
            )
            return
        if path == f"{task_base}/fail":
            self._respond(
                {
                    "task_id": self.server.task_id,
                    "status": "failed",
                    "accepted": True,
                }
            )
            return
        self.send_error(404)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _RunnerHttpServer(ThreadingHTTPServer):
    def __init__(self, *, bootstrap, task, task_id, claim_id, execution_token):
        super().__init__(("127.0.0.1", 0), _RunnerApiHandler)
        self.bootstrap_payload = bootstrap
        self.task_payload = task
        self.task_id = task_id
        self.claim_id = claim_id
        self.execution_token = execution_token
        self.requests: list[tuple[str, str, str | None, dict[str, object] | None]] = []


@pytest.mark.asyncio
async def test_supervisor_still_requires_explicit_execution_adapter_config(
    monkeypatch,
    tmp_path,
):
    workdir = tmp_path / "work"
    workdir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    missing_config = project_root / "missing-config.toml"
    client = SimpleNamespace(
        server_url="https://simstack.example/api",
        bootstrap=AsyncMock(
            return_value={
                **_bootstrap(http_success_node, workdir),
                "scopes": list(RUNNER_SUPERVISOR_SCOPES),
            }
        ),
    )
    monkeypatch.setattr(GlobalState, "_instance", None)
    monkeypatch.setattr(
        SupervisorRunnerClient,
        "from_environment",
        classmethod(lambda cls, **_kwargs: client),
    )
    monkeypatch.setattr(
        "simstack.runner_runtime.environment.assert_database_runtime_unavailable",
        lambda: None,
    )

    with pytest.raises(
        RuntimeError,
        match="execution-adapter config does not exist",
    ):
        await GlobalState().initialize_runner(
            resource="local",
            project_root=project_root,
            config_file=missing_config,
        )

    assert not missing_config.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function", "expected_success", "terminal_operation"),
    [
        (http_success_node, True, "commit"),
        (http_legacy_db_node, False, "fail"),
    ],
)
async def test_localhost_http_worker_executes_only_through_runner_api(
    monkeypatch,
    tmp_path,
    function,
    expected_success,
    terminal_operation,
):
    task_id = "507f1f77bcf86cd799439011"
    claim_id = "claim-1"
    execution_token = "execution-token"
    workdir = tmp_path / "work"
    workdir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    server = _RunnerHttpServer(
        bootstrap=_bootstrap(function, workdir),
        task=_task_bundle(function, task_id),
        task_id=task_id,
        claim_id=claim_id,
        execution_token=execution_token,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    previous_instance = GlobalState._instance
    GlobalState._instance = None
    runner_context = GlobalState()
    previous_context = context_module.context
    previous_run_node_context = run_node_module.context
    previous_task_execution_context = task_execution_module.context
    previous_task_mapping_context = task_mapping_module.context
    previous_nested_execution_context = nested_execution_module.context
    context_module.context = runner_context
    run_node_module.context = runner_context
    task_execution_module.context = runner_context
    task_mapping_module.context = runner_context
    nested_execution_module.context = runner_context
    monkeypatch.setattr(
        "simstack.runner_runtime.environment.assert_database_runtime_unavailable",
        lambda: None,
    )
    monkeypatch.setattr(
        GlobalState,
        "initialize_logging",
        lambda self, **_kwargs: None,
    )

    original_import = builtins.__import__
    persistence_only_modules = {
        "simstack.core.artifacts",
        "simstack.core.node_persistence",
        "simstack.core.process_results",
        "simstack.core.recompute_artifacts",
    }

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".", 1)[0] in {"bson", "motor", "odmantic", "pymongo"}:
            raise AssertionError(f"worker imported database runtime: {name}")
        if name in persistence_only_modules:
            raise AssertionError(
                f"worker imported persistence-only framework module: {name}"
            )
        return original_import(name, globals, locals, fromlist, level)

    original_environment = dict(os.environ)
    os.environ.clear()
    os.environ.update(
        {
            "HOME": original_environment.get("HOME", str(tmp_path)),
            "PATH": original_environment.get("PATH", "/usr/bin:/bin"),
            "SIMSTACK_SERVER_URL": (f"http://127.0.0.1:{server.server_address[1]}"),
            "SIMSTACK_RUNNER_TOKEN": execution_token,
            "SIMSTACK_WORKER_ENVIRONMENT_ALLOWLIST": "[]",
        }
    )
    builtins.__import__ = guarded_import
    try:
        succeeded = await run_node_module.run_node_from_id(
            task_id,
            "local",
            project_root,
            claim_id=claim_id,
            attempt=1,
        )
    finally:
        builtins.__import__ = original_import
        os.environ.clear()
        os.environ.update(original_environment)
        context_module.context = previous_context
        run_node_module.context = previous_run_node_context
        task_execution_module.context = previous_task_execution_context
        task_mapping_module.context = previous_task_mapping_context
        nested_execution_module.context = previous_nested_execution_context
        GlobalState._instance = previous_instance
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert succeeded is expected_success
    task_base = f"/api/runner/tasks/{task_id}"
    assert [(method, path) for method, path, _, _ in server.requests] == [
        ("GET", f"{task_base}/bootstrap"),
        ("GET", task_base),
        ("POST", f"{task_base}/status"),
        ("POST", f"{task_base}/cache/resolve"),
        ("POST", f"{task_base}/{terminal_operation}"),
    ]
    assert all(
        authorization == f"Bearer {execution_token}"
        for _, _, authorization, _ in server.requests
    )
    assert not any(
        "database" in path or "/db" in path for _, path, _, _ in server.requests
    )
    assert not (project_root / "config.toml").exists()
    with pytest.raises(RuntimeError, match="only available to the API-only supervisor"):
        _ = runner_context.runner_adapter
