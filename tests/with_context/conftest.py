"""Core scientific integration tests use the actual Server domain HTTP API.

Run with the Server development dependencies and both source checkouts on
PYTHONPATH. Database setup/assertions stay in this fixture, outside Runner code.
"""
from pathlib import Path
import importlib.util
import sys

import pytest
import pytest_asyncio

from simstack.core.context import context
from simstack.models.files import FileStack
from simstack.util.file_transfer_client import FileTransferClient
from simstack.util.object_transfer import ObjectFileClient

real_object_client_for_api = ObjectFileClient.for_api


@pytest_asyncio.fixture
async def domain_bridge(monkeypatch, tmp_path):
    server_root = Path(__file__).resolve().parents[3] / "simstack-server"
    if not server_root.is_dir():
        raise RuntimeError("Scientific HTTP integration tests require the simstack-server source checkout")
    monkeypatch.syspath_prepend(str(server_root))
    helper_path = server_root / "tests" / "domain_http_fixture.py"
    spec = importlib.util.spec_from_file_location("simstack_domain_test_bridge", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    async with helper.domain_client(monkeypatch, tmp_path) as bridge:
        from server.routes import user_runtime
        from server.auth import get_current_user
        from simstack.server_api import ServerAPI
        app = bridge.http._transport.app
        app.include_router(user_runtime.router)
        app.dependency_overrides[get_current_user] = lambda: bridge.auth.user
        user_transport = type(bridge.api.requests.transport)()
        user_transport.rotate_credentials = False
        user_api = ServerAPI(user_transport, authorization="user", resource="test")
        user_api.requests.bootstrap_data = bridge.api.requests.bootstrap_data

        bridge.runner_api = bridge.api
        bridge.user_api = user_api
        monkeypatch.setattr(bridge.api.tasks, "submit_root", user_api.tasks.submit_root)
        runner_resolve = bridge.api.tasks.resolve_invocation
        async def resolve_invocation(payload, *, request_id):
            method = runner_resolve if payload.get("parent_execution") else user_api.tasks.resolve_invocation
            return await method(payload, request_id=request_id)
        monkeypatch.setattr(bridge.api.tasks, "resolve_invocation", resolve_invocation)
        runner_reuse = bridge.api.tasks.reuse_invocation
        async def reuse_invocation(payload, *, request_id):
            method = runner_reuse if payload.get("parent_execution") else user_api.tasks.reuse_invocation
            return await method(payload, request_id=request_id)
        monkeypatch.setattr(bridge.api.tasks, "reuse_invocation", reuse_invocation)
        def domain_errors(original):
            async def json_with_domain_errors(method, path, body=None, **kwargs):
                import httpx
                from simstack.server_api.http_transport import RunnerAPIError
                try:
                    return await original(method, path, body, **kwargs)
                except httpx.HTTPStatusError as exc:
                    detail = exc.response.json().get("detail", "Runner API request failed")
                    raise RunnerAPIError(f"Runner API returned HTTP {exc.response.status_code}: {detail}", status_code=exc.response.status_code) from None
            return json_with_domain_errors

        for transport in (bridge.api.requests.transport, user_transport):
            monkeypatch.setattr(transport, "json", domain_errors(transport.json))
        # File publications use actual object control-plane routes and a local
        # S3 HTTP fixture, rather than silently seeding scientific metadata.
        from server.routes import file_transfers, object_files
        from server.runner_auth import get_current_runner_context
        app.include_router(object_files.router)
        app.include_router(file_transfers.router)
        bridge.auth.scopes.update({"files:upload", "files:download", "file:status", "file:download", "file:complete"})
        app.dependency_overrides[get_current_runner_context] = lambda: bridge.auth
        object_generator = None

        def object_client_for_api(api):
            nonlocal object_generator
            if object_generator is None:
                object_spec = importlib.util.spec_from_file_location("simstack_domain_object_fixture",
                    server_root / "tests" / "object_file_fakes.py")
                object_helper = importlib.util.module_from_spec(object_spec)
                object_spec.loader.exec_module(object_helper)
                object_generator = object_helper.object_store.__wrapped__(monkeypatch)
                bridge.object_store = next(object_generator)
            return real_object_client_for_api(api)

        import asyncio
        loop = asyncio.get_running_loop()

        def object_request(client, method, path, payload=None, **kwargs):
            request = bridge.http.request(method, path, json=payload, headers=client._auth_headers())
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                # Sync workflow file reads run inside the nested SDK event loop.
                response = loop.run_until_complete(request)
            else:
                response = asyncio.run_coroutine_threadsafe(request, loop).result(timeout=15)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(ObjectFileClient, "for_api", object_client_for_api)
        monkeypatch.setattr(ObjectFileClient, "_json_request", object_request)
        monkeypatch.setattr(FileTransferClient, "_json_request", object_request)
        from simstack.models.document import Model
        from simstack.models.document.models import _MODEL_TYPES
        from simstack.tables.catalogue import CatalogueCollector
        from simstack.tables.model_table import CreateModelTable
        from simstack.tables.node_table import CreateNodeTable
        collector = CatalogueCollector()
        core_root = Path(__file__).resolve().parents[2]
        catalogue_roots = [core_root / "src", core_root / "tests" / "with_context", core_root / "tests" / "data"]
        modules = set()
        for module in list(sys.modules.values()):
            source = getattr(module, "__file__", None)
            if source and any(Path(source).resolve().is_relative_to(root) for root in catalogue_roots):
                modules.add(module)
        model_builder = CreateModelTable(collector, project_root=Path(__file__).resolve().parents[2])
        node_builder = CreateNodeTable(collector, project_root=Path(__file__).resolve().parents[2])
        for module in sorted(modules, key=lambda value: value.__name__):
            await model_builder._process_module(module, "")
        for module in sorted(modules, key=lambda value: value.__name__):
            await node_builder._process_module(module, "")
        from server.storage_models import storage_model_mapping
        for descriptor in collector.models.values():
            await bridge.database.save(storage_model_mapping(
                descriptor, model_class=collector.model_types[descriptor.mapping],
            ))
        for descriptor in collector.nodes.values():
            await bridge.database.save(descriptor)
        await bridge.database.save(bridge.auth.resource)
        try:
            yield bridge
        finally:
            if object_generator is not None:
                try:
                    next(object_generator)
                except StopIteration:
                    pass


@pytest_asyncio.fixture(autouse=True)
async def initialized_context(domain_bridge, monkeypatch, tmp_path):
    from simstack.util.config_reader import ConfigReader
    from simstack.util.resource_config import ResourceConfig
    from simstack.core.node import Node
    from simstack.core.definitions import TaskStatus
    from simstack.util.mappings import ModelMappingTable, NodeMappingTable

    api = domain_bridge.api
    await api.resource.bootstrap()
    monkeypatch.setattr(context, "_server", api)
    monkeypatch.setattr(context, "_initialized", True)
    monkeypatch.setattr(context, "_config", ConfigReader(domain_bridge.auth.resource,
        project_root=Path(__file__).resolve().parents[2], server_url=api.requests.server_url,
        server_token=api.requests.token, account_id=api.requests.bootstrap_data["account_id"]))
    monkeypatch.setattr(context, "_resource_config", ResourceConfig(tmp_path / "config.toml", "test"))
    await context.refresh_mappings()

    async def execute_in_process(node):
        from simstack.runner_runtime.launch_claims import task_execution
        with task_execution(node.registry_entry):
            result = await node.execute_node_locally()
        if node.registry_entry.status == TaskStatus.FAILED and node.registry_entry.return_kind == "bool":
            raise RuntimeError("node returned False")
        return result

    monkeypatch.setattr(Node, "run_node_as_process", execute_in_process)
    original_dispatch = Node.run_somewhere

    async def dispatch_local(node):
        entry = node.registry_entry
        if (entry is not None and entry.status == TaskStatus.SUBMITTED
                and str(entry.parameters.resource) == str(context.config.resource)
                and entry.parameters.queue == "default" and not entry.parameters.in_docker):
            from simstack.runner_runtime.launch_claims import acquire_work
            acquired = await acquire_work(domain_bridge.runner_api, capacity=1000)
            claimed = next((candidate for candidate in acquired if candidate.id == entry.id), None)
            if claimed is None:
                raise AssertionError("The submitted local task was not acquired by its Runner")
            node.registry_entry = claimed
            node.parameters = claimed.parameters
            node._owns_inline_nested_execution = True
        return await original_dispatch(node)

    monkeypatch.setattr(Node, "run_somewhere", dispatch_local)
    yield context


@pytest.fixture
def server_storage(domain_bridge):
    return domain_bridge.database


@pytest.fixture
def test_file_stack(initialized_context):
    path = context.config.workdir / "test_file.txt"
    path.write_text("test content")
    yield FileStack.from_local_file(path, is_hashable=True)
    path.unlink(missing_ok=True)
