import asyncio
from collections import defaultdict, deque
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import ssl
import threading
from types import SimpleNamespace
import urllib.error
from unittest.mock import AsyncMock

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.node import Node
from simstack.models import FloatData
from simstack.runner_api.transport import (
    RunnerAPIClient,
    RunnerAPIError,
)


@pytest.fixture
def api_server(monkeypatch):
    monkeypatch.delenv("SIMSTACK_RUNNER_CREDENTIALS", raising=False)

    @contextmanager
    def serve(routes):
        replies = {key: deque(values) for key, values in routes.items()}
        requests = defaultdict(list)

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                key = (self.command, self.path)
                requests[key].append(json.loads(body) if body else None)
                queue = replies[key]
                status, payload = queue.popleft() if len(queue) > 1 else queue[0]
                encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = handle_request
            do_POST = handle_request
            do_DELETE = handle_request

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    return serve


@pytest.mark.parametrize("method,path", [
    ("GET", "/runner/control"),
    ("POST", "/runner/data/query"),
])
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_safe_read_retries_transient_http_error(api_server, method, path, status):
    body = {"model": "node_registry"} if method == "POST" else None
    key = (method, path)
    with api_server({key: [(status, {}), (200, {"documents": []})]}) as (base, requests):
        api = RunnerAPIClient(base, "test-token", read_retry_delay=0)
        assert api.json_sync(method, path, body) == {"documents": []}
        assert requests[key] == [body, body]


def test_persistent_outage_has_bounded_backoff_and_explicit_read_error(api_server, monkeypatch):
    delays = []
    monkeypatch.setattr("simstack.runner_api.transport.time.sleep", delays.append)
    key = ("POST", "/runner/data/query")
    with api_server({key: [(502, {})]}) as (base, requests):
        api = RunnerAPIClient(base, "test-token")
        with pytest.raises(RunnerAPIError, match="HTTP 502"):
            api.json_sync(*key, {"model": "node_registry"})
        assert len(requests[key]) == 3
        assert delays == [1, 2]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_read_error_is_not_retried(api_server, monkeypatch, status):
    monkeypatch.setattr("simstack.runner_api.credentials.refreshed_token", lambda *args: None)
    key = ("POST", "/runner/data/query")
    with api_server({key: [(status, {}), (200, {"documents": []})]}) as (base, requests):
        api = RunnerAPIClient(base, "test-token", read_retry_delay=0)
        with pytest.raises(RunnerAPIError, match=f"HTTP {status}"):
            api.json_sync(*key, {})
        assert len(requests[key]) == 1


@pytest.mark.parametrize("method,path", [
    ("POST", "/submit-node/"),
    ("POST", "/runner/data/save"),
    ("POST", "/runner/tasks/child/mutation"),
    ("POST", "/runner/data/delete"),
])
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.asyncio
async def test_ambiguous_write_is_attempted_once(api_server, method, path, use_async):
    key = (method, path)
    # Each request represents a server-side mutation, even if its response is 502.
    with api_server({key: [(502, {}), (200, {"accepted": True})]}) as (base, requests):
        api = RunnerAPIClient(base, "test-token", read_retry_delay=0)
        with pytest.raises(RunnerAPIError):
            if use_async:
                await api.json(method, path, {"id": "same-task"})
            else:
                api.json_sync(method, path, {"id": "same-task"})
        assert requests[key] == [{"id": "same-task"}]


@pytest.mark.parametrize("error", [
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
    urllib.error.URLError(ConnectionResetError("reset")),
])
def test_safe_read_retries_network_interruption(monkeypatch, error):
    calls = []

    def urlopen(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise error
        return io.BytesIO(b'{"documents":[]}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    api = RunnerAPIClient("https://example.test/api", "test-token", read_retry_delay=0)
    assert api.json_sync("POST", "/runner/data/query", {}) == {"documents": []}
    assert len(calls) == 2


def test_certificate_failure_is_not_retried(monkeypatch):
    calls = []

    def urlopen(request, **kwargs):
        calls.append(request)
        raise urllib.error.URLError(ssl.SSLCertVerificationError("untrusted certificate"))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    api = RunnerAPIClient("https://example.test/api", "test-token", read_retry_delay=0)
    with pytest.raises(RunnerAPIError):
        api.json_sync("GET", "/runner/control")
    assert len(calls) == 1


def test_truncated_gateway_error_body_still_retries_the_read(monkeypatch):
    class TruncatedBody(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"bad gateway")

    calls = []

    def urlopen(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, TruncatedBody())
        return io.BytesIO(b'{"documents":[]}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    api = RunnerAPIClient("https://example.test/api", "test-token", read_retry_delay=0)
    assert api.json_sync("POST", "/runner/data/query", {}) == {"documents": []}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancelled_async_read_does_not_issue_another_attempt(api_server, monkeypatch):
    backing_off = asyncio.Event()
    never = asyncio.Event()

    async def wait(delay):
        backing_off.set()
        await never.wait()

    monkeypatch.setattr("simstack.runner_api.transport.asyncio.sleep", wait)
    key = ("POST", "/runner/data/query")
    with api_server({key: [(502, {}), (200, {"documents": []})]}) as (base, requests):
        api = RunnerAPIClient(base, "test-token")
        task = asyncio.create_task(api.json(*key, {}))
        await asyncio.wait_for(backing_off.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(requests[key]) == 1


@pytest.mark.asyncio
async def test_remote_wait_recovers_status_read_without_resubmitting(api_server, monkeypatch):
    submit = ("POST", "/submit-node/")
    query = ("POST", "/runner/data/query")
    routes = {
        submit: [(200, {"id": "remote-task"})],
        query: [
            (502, {}),
            (200, {"documents": [{"status": "running"}]}),
            (200, {"documents": [{"status": "completed"}]}),
        ],
    }
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr("simstack.core.node.asyncio.sleep", fast_sleep)
    with api_server(routes) as (base, requests):
        api = RunnerAPIClient(base, "test-token", read_retry_delay=0)

        async def load_task_by_id(identity):
            response = await api.json(*query, {"task_id": identity})
            return SimpleNamespace(id=identity, status=TaskStatus(response["documents"][0]["status"]))

        monkeypatch.setattr("simstack.core.node.context", SimpleNamespace(
            db=SimpleNamespace(load_task_by_id=load_task_by_id),
        ))
        launched = await api.json(*submit, {"id": "remote-task"})
        execution = Node.__new__(Node)
        execution.name = "remote_probe"
        execution.registry_entry = SimpleNamespace(id=launched["id"])
        value = FloatData(value=42)
        execution.load_results = AsyncMock(return_value=value)

        result = await execution._wait_for_remote_completion()

        assert result is value
        assert execution.registry_entry.status == TaskStatus.COMPLETED
        execution.load_results.assert_awaited_once()
        assert len(requests[submit]) == 1
        assert len(requests[query]) == 3
