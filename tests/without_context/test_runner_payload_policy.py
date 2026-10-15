"""Only advertised operator transport policies limit runner JSON payloads."""

import io
import json
from unittest.mock import MagicMock

import pytest

from simstack.runner_api.errors import RunnerApiError
from simstack.runner_api.transport import RunnerHttpTransport
from simstack.runner_api.config import FILE_TRANSFER_POLL_ENV, FileTransferApiSettings
from simstack.runner_api.errors import RunnerApiConfigurationError
from simstack.runner_protocol.dto.bootstrap import RunnerCapabilitiesV1


def _transport(monkeypatch, payload):
    stream = io.BytesIO(json.dumps(payload).encode())
    response = MagicMock(status=200)
    response.read.side_effect = stream.read
    connection = MagicMock()
    connection.getresponse.return_value = response
    transport = RunnerHttpTransport(server_url="http://127.0.0.1", token=lambda: "test-token", timeout_seconds=5, retry_attempts=0, retry_backoff_seconds=0)
    monkeypatch.setattr(transport, "connection", lambda: connection)
    return transport, response, connection


def test_unbounded_transport_and_physical_inline_capacity_are_independent():
    unbounded = RunnerCapabilitiesV1(max_runner_payload_bytes=None, max_inline_document_bytes=1024)
    assert unbounded.max_runner_payload_bytes is None
    bounded = RunnerCapabilitiesV1(max_runner_payload_bytes=10, max_inline_document_bytes=1024)
    assert bounded.max_inline_document_bytes == 1024


def test_absent_policy_reads_a_valid_response_without_a_guessed_limit(monkeypatch):
    value = {"content": "x" * 4096}
    transport, response, connection = _transport(monkeypatch, value)
    assert transport.request("GET", "/bootstrap", None) == value
    response.read.assert_called_once_with()
    connection.close.assert_called_once()


def test_explicit_response_policy_is_checked_before_decoding(monkeypatch):
    transport, response, connection = _transport(monkeypatch, {"content": "x" * 128})
    transport.max_payload_bytes = 10
    with pytest.raises(RunnerApiError, match="response exceeds"):
        transport.request("GET", "/bootstrap", None)
    response.read.assert_called_once_with(11)
    connection.close.assert_called_once()


def test_exact_response_boundary_is_allowed(monkeypatch):
    value = {"content": "value"}
    transport, _, _ = _transport(monkeypatch, value)
    transport.max_payload_bytes = len(json.dumps(value).encode())
    assert transport.request("GET", "/bootstrap", None) == value


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_poll_configuration_rejects_nonfinite_or_nonpositive_values(monkeypatch, value):
    monkeypatch.setenv(FILE_TRANSFER_POLL_ENV, value)
    with pytest.raises(RunnerApiConfigurationError, match="finite and positive"):
        FileTransferApiSettings.from_environment()
