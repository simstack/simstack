"""Recover a scientific write without repeating its accepted mutation."""
import asyncio
from copy import deepcopy

import pytest

from simstack.server_api.http_transport import RunnerAPIError
from simstack.server_api.server_requests import ServerRequests


class ReceiptTransport:
    server_url = "https://example.test/api"
    token = "test-token"

    def __init__(self, *, pending=False, read_failures=0, status=None, never_applied=False):
        self.pending = pending
        self.read_failures = read_failures
        self.status = status
        self.never_applied = never_applied
        self.posts = []
        self.reads = []
        self.mutations = 0
        self.receipt = None
        self.after_first_post = None

    async def json(self, method, path, body=None):
        if method == "GET":
            self.reads.append(path)
            if self.read_failures:
                self.read_failures -= 1
                raise RunnerAPIError("Receipt unavailable", status_code=503)
            return self.receipt or {"state": "not_found"}
        self.posts.append(deepcopy(body))
        if self.status:
            raise RunnerAPIError("Rejected", status_code=self.status)
        if self.after_first_post and len(self.posts) == 1:
            self.after_first_post()
        if self.never_applied:
            self.receipt = {"state": "pending"}
            raise RunnerAPIError("Worker unavailable", status_code=503)
        if self.pending and len(self.posts) == 1:
            self.receipt = {"state": "pending"}
            raise RunnerAPIError("Lost response")
        if self.receipt is None or self.receipt["state"] != "applied":
            self.mutations += 1
            self.receipt = {"state": "applied", "result": {"accepted": deepcopy(body["graph"])}}
        # The mutation commits but the response is lost on every POST.
        raise RunnerAPIError("Runner API write outcome may be indeterminate")


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_lost_publication_reply_returns_receipt_without_duplicate_science(pending):
    transport = ReceiptTransport(pending=pending)
    requests = ServerRequests(transport, command_recovery_timeout=1)
    result = await requests.command("/values/publications", {"graph": {"energy": -3.25}}, recover=True)
    assert result == {"accepted": {"energy": -3.25}}
    assert transport.mutations == 1
    assert len({post["request_id"] for post in transport.posts}) == 1
    assert all(path.endswith("/requests/" + transport.posts[0]["request_id"]) for path in transport.reads)


@pytest.mark.asyncio
async def test_receipt_reads_can_fail_transiently_without_losing_publication_identity():
    transport = ReceiptTransport(read_failures=2)
    result = await ServerRequests(transport, command_recovery_timeout=2).command(
        "/values/publications", {"graph": {"values": [1, 2]}}, recover=True)
    assert result == {"accepted": {"values": [1, 2]}}
    assert transport.mutations == 1
    assert len(transport.posts) == 1
    assert len(transport.reads) == 3


@pytest.mark.asyncio
async def test_pending_replay_preserves_original_payload_when_caller_mutates_it():
    original = {"graph": {"values": [1, 2]}}
    transport = ReceiptTransport(pending=True)
    transport.after_first_post = lambda: original["graph"]["values"].append(99)
    result = await ServerRequests(transport, command_recovery_timeout=1).command(
        "/values/publications", original, recover=True)
    assert original["graph"]["values"] == [1, 2, 99]
    assert result == {"accepted": {"values": [1, 2]}}
    assert transport.posts[0] == transport.posts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 409, 422])
async def test_true_command_rejection_is_not_retried(status):
    transport = ReceiptTransport(status=status)
    with pytest.raises(RunnerAPIError) as caught:
        await ServerRequests(transport).command("/values/publications", {"graph": {}}, recover=True)
    assert caught.value.status_code == status
    assert len(transport.posts) == 1
    assert not transport.reads


@pytest.mark.asyncio
async def test_pending_command_exhaustion_retains_endpoint_and_request_identity():
    transport = ReceiptTransport(never_applied=True)
    with pytest.raises(RunnerAPIError, match="indeterminate") as caught:
        await ServerRequests(transport, command_recovery_timeout=0.02).command(
            "/values/publications", {"graph": {}}, recover=True)
    assert "/values/publications" in str(caught.value)
    assert transport.posts[0]["request_id"] in str(caught.value)
    assert len({post["request_id"] for post in transport.posts}) == 1
    assert transport.mutations == 0


@pytest.mark.asyncio
async def test_non_opted_in_command_keeps_existing_single_send_behavior():
    transport = ReceiptTransport()
    with pytest.raises(RunnerAPIError):
        await ServerRequests(transport).command("/events", {"graph": {}})
    assert len(transport.posts) == 1
    assert not transport.reads


@pytest.mark.asyncio
async def test_rejected_receipt_propagates_canonical_rejection_without_replay():
    class RejectedTransport(ReceiptTransport):
        async def json(self, method, path, body=None):
            if method == "GET":
                self.reads.append(path)
                return {"state": "rejected", "status_code": 422, "detail": "Invalid scientific value"}
            self.posts.append(deepcopy(body))
            raise RunnerAPIError("Lost rejection reply")

    transport = RejectedTransport()
    with pytest.raises(RunnerAPIError, match="Invalid scientific value") as caught:
        await ServerRequests(transport).command("/values/publications", {"graph": {}}, recover=True)
    assert caught.value.status_code == 422
    assert len(transport.posts) == 1


@pytest.mark.asyncio
async def test_missing_receipt_resumes_same_scientific_command_after_server_restart():
    class RestartTransport(ReceiptTransport):
        async def json(self, method, path, body=None):
            if method == "POST" and not self.posts:
                self.posts.append(deepcopy(body))
                raise RunnerAPIError("Server restarted before receipt creation", status_code=503)
            return await super().json(method, path, body)

    transport = RestartTransport()
    result = await ServerRequests(transport, command_recovery_timeout=1).command(
        "/values/publications", {"graph": {"energy": -4.5}}, recover=True)
    assert result == {"accepted": {"energy": -4.5}}
    assert transport.posts[0] == transport.posts[1]
    assert transport.mutations == 1


@pytest.mark.asyncio
async def test_scientific_publication_decodes_canonical_graph_after_lost_response():
    from simstack.models import StringData
    from simstack.server_api.scientific_values import ScientificValuesAPI

    class GraphTransport(ReceiptTransport):
        async def json(self, method, path, body=None):
            if method == "GET":
                result = await super().json(method, path, body)
                if result.get("state") == "applied":
                    return {"state": "applied", "result": {"graph": result["result"]["accepted"]}}
                return result
            return await super().json(method, path, body)

    transport = GraphTransport()
    original = StringData(value="literal metadata", field_name="opaque value")
    restored = await ScientificValuesAPI(ServerRequests(transport)).publish_value(original)
    assert restored.model_dump() == original.model_dump()
    assert transport.mutations == 1
    assert transport.reads


@pytest.mark.asyncio
async def test_original_delayed_commit_wins_over_pending_replay():
    class DelayedTransport(ReceiptTransport):
        def __init__(self):
            super().__init__()
            self.commit = None

        async def finish_original(self, graph):
            await asyncio.sleep(0.01)
            self.mutations += 1
            self.receipt = {"state": "applied", "result": {"accepted": graph}}

        async def json(self, method, path, body=None):
            if method == "GET":
                return await super().json(method, path, body)
            self.posts.append(deepcopy(body))
            if self.commit is None:
                self.receipt = {"state": "pending"}
                self.commit = asyncio.create_task(self.finish_original(deepcopy(body["graph"])))
                raise RunnerAPIError("Original response exceeded socket deadline")
            raise RunnerAPIError("Original request still owns its lease", status_code=503)

    transport = DelayedTransport()
    result = await ServerRequests(transport, command_recovery_timeout=1).command(
        "/values/publications", {"graph": {"energy": -6.5}}, recover=True)
    await transport.commit
    assert result == {"accepted": {"energy": -6.5}}
    assert transport.mutations == 1
    assert all(post == transport.posts[0] for post in transport.posts)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_recovery_budget_must_be_positive_and_finite(timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        ServerRequests(ReceiptTransport(), command_recovery_timeout=timeout)
