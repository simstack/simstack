from __future__ import annotations

import io
import json
from unittest.mock import Mock

import pytest

from simstack.workflows import cli
from simstack.workflows.cli import InstallationClient, _NoRedirect


def test_cli_upload_uses_http_multipart_and_explicit_target_resources(tmp_path):
    (tmp_path / "simstack-workflow.toml").write_text("[workflow]\n")
    client = InstallationClient("http://localhost:8001", "user-token")
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return io.BytesIO(json.dumps({"id": "test", "status": "staged"}).encode())

    client._opener = Opener()
    result = client.upload(tmp_path, ["cpu", "cluster"])
    request = calls[0]
    assert request.full_url == "http://localhost:8001/workflow-installations"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer user-token"
    assert b'name="resources"' in request.data
    assert b'["cpu", "cluster"]' in request.data
    assert b'filename="workflow.zip"' in request.data
    assert result["status"] == "staged"
    assert "user-token" not in request.data.decode(errors="replace")


def test_cli_uv_folder_upload_materializes_locked_sources(tmp_path, monkeypatch):
    from simstack.workflows import snapshot

    (tmp_path / "simstack-workflow.toml").write_text("[workflow]\n")
    (tmp_path / "uv.lock").write_text("version=1\n")
    prepared = cli.snapshot_folder(tmp_path)
    calls = []

    def build(source, output):
        calls.append(source)
        output.write_bytes(prepared)

    monkeypatch.setattr(snapshot, "build_snapshot", build)
    client = InstallationClient("https://example.test/api", "user-token")
    request = Mock(return_value={"status": "staged"})
    monkeypatch.setattr(client, "request", request)
    assert client.upload(tmp_path, ["cpu"])["status"] == "staged"
    assert calls == [tmp_path]
    assert prepared in request.call_args.kwargs["body"]


def test_cli_prepared_uv_snapshot_is_uploaded_without_resolving_again(
    tmp_path, monkeypatch
):
    from simstack.workflows import snapshot

    (tmp_path / "simstack-workflow.toml").write_text("[workflow]\n")
    (tmp_path / "uv.lock").write_text("version=1\n")
    (tmp_path / ".simstack").mkdir()
    (tmp_path / ".simstack/snapshot.json").write_text('{"version":1,"sources":[]}')
    builder = Mock(
        side_effect=AssertionError("Prepared source identity must be preserved")
    )
    monkeypatch.setattr(snapshot, "build_snapshot", builder)
    client = InstallationClient("https://example.test/api", "user-token")
    monkeypatch.setattr(client, "request", Mock(return_value={"status": "staged"}))
    assert client.upload(tmp_path, ["cpu"])["status"] == "staged"
    builder.assert_not_called()


def test_cli_preserves_explicit_operator_base_path_without_api_guessing():
    assert (
        InstallationClient("https://example.test/custom/", "token").base
        == "https://example.test/custom"
    )


def test_cli_wait_stops_at_preview_and_confirmation_is_separate(monkeypatch):
    client = InstallationClient("https://example.test/server", "token")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        return {"id": "a/b", "status": "staged"}

    monkeypatch.setattr(client, "request", request)
    assert client.wait("a/b", timeout=1, interval=1)["status"] == "staged"
    assert calls == [("GET", "/workflow-installations/a%2Fb")]
    client.confirm("a/b")
    assert calls[-1] == ("POST", "/workflow-installations/a%2Fb/confirm")


def test_cli_wait_returns_failed_resource_promptly_without_sleep(monkeypatch):
    client = InstallationClient("https://example.test", "token")
    result = {
        "id": "failed-target",
        "status": "installing",
        "resources": [
            {"resource": "cpu", "status": "failed", "error": "Missing profile"}
        ],
    }
    status = Mock(return_value=result)
    sleep = Mock(side_effect=AssertionError("Failed preparations must not wait"))
    monkeypatch.setattr(client, "status", status)
    monkeypatch.setattr(cli.time, "monotonic", Mock(return_value=0))
    monkeypatch.setattr(cli.time, "sleep", sleep)

    assert client.wait("failed-target", timeout=900, interval=2) == result

    status.assert_called_once_with("failed-target")
    sleep.assert_not_called()


def test_cli_wait_continues_while_pending_and_stops_when_active(monkeypatch):
    client = InstallationClient("https://example.test", "token")
    status = Mock(
        side_effect=[
            {
                "id": "job",
                "status": "installing",
                "resources": [{"resource": "cpu", "status": "pending"}],
            },
            {
                "id": "job",
                "status": "active",
                "resources": [{"resource": "cpu", "status": "ready"}],
            },
        ]
    )
    sleep = Mock()
    monkeypatch.setattr(client, "status", status)
    monkeypatch.setattr(cli.time, "monotonic", Mock(return_value=0))
    monkeypatch.setattr(cli.time, "sleep", sleep)

    assert client.wait("job", timeout=900, interval=2)["status"] == "active"

    assert status.call_count == 2
    sleep.assert_called_once_with(2)


def _main_client(
    monkeypatch, arguments, *, upload=None, waits=(), confirmation=None, status=None
):
    calls = []
    wait_results = iter(waits)

    class Client:
        def __init__(self, server_url, token, *, timeout):
            assert server_url == "https://example.test/api"
            assert token == "user-token"

        def upload(self, source, resources):
            calls.append("upload")
            assert source == cli.Path("package")
            assert resources == ["cpu"]
            return upload

        def wait(self, identifier, *, timeout, interval):
            calls.append("wait")
            assert identifier == "job"
            return next(wait_results)

        def confirm(self, identifier):
            calls.append("confirm")
            assert identifier == "job"
            return confirmation

        def status(self, identifier):
            calls.append("status")
            return status

        def request(self, method, path):
            calls.append("list")
            assert method == "GET"
            assert path == "/workflow-installations"
            return [status]

    monkeypatch.setattr(cli, "InstallationClient", Client)
    monkeypatch.setattr(
        "sys.argv",
        [
            "simstack_workflow",
            "--server-url",
            "https://example.test/api",
            "--token",
            "user-token",
            *arguments,
        ],
    )
    return calls


@pytest.mark.parametrize(
    ("flags", "expected_calls", "expected_status"),
    [
        ([], ["upload"], "staged"),
        (["--wait"], ["upload", "wait"], "staged"),
        (["--confirm"], ["upload", "wait", "confirm"], "installing"),
        (["--confirm", "--wait"], ["upload", "wait", "confirm", "wait"], "active"),
    ],
)
def test_main_explicit_upload_flags_preserve_preview_and_wait_after_confirmation(
    monkeypatch, capsys, flags, expected_calls, expected_status
):
    calls = _main_client(
        monkeypatch,
        ["upload", "package", "--resource", "cpu", *flags],
        upload={"id": "job", "status": "staged"},
        waits=[{"id": "job", "status": "staged"}, {"id": "job", "status": "active"}],
        confirmation={"id": "job", "status": "installing"},
    )

    cli.main()

    assert calls == expected_calls
    assert json.loads(capsys.readouterr().out)["status"] == expected_status


@pytest.mark.parametrize(
    ("result", "expected_error"),
    [
        (
            {"id": "job", "status": "failed", "error": "Invalid return contract"},
            "Invalid return contract",
        ),
        (
            {
                "id": "job",
                "status": "installing",
                "resources": [
                    {
                        "resource": "cpu",
                        "status": "failed",
                        "error": "Wheel unavailable",
                    },
                ],
            },
            "cpu: Wheel unavailable",
        ),
    ],
)
def test_main_wait_errors_exit_nonzero_with_actionable_server_failure(
    monkeypatch, capsys, result, expected_error
):
    _main_client(monkeypatch, ["wait", "job"], waits=[result])

    with pytest.raises(SystemExit) as failure:
        cli.main()

    assert failure.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert expected_error in output.err
    assert "Correct" in output.err


def test_main_does_not_confirm_a_failed_preview(monkeypatch, capsys):
    result = {"id": "job", "status": "failed", "error": "Unknown output model"}
    calls = _main_client(
        monkeypatch,
        ["upload", "package", "--resource", "cpu", "--confirm", "--wait"],
        upload=result,
        waits=[result],
    )

    with pytest.raises(SystemExit) as failure:
        cli.main()

    assert failure.value.code == 1
    assert calls == ["upload", "wait"]
    assert "Unknown output model" in capsys.readouterr().err


def test_main_confirm_wait_reports_resource_failure_after_delivery_started(
    monkeypatch, capsys
):
    calls = _main_client(
        monkeypatch,
        ["upload", "package", "--resource", "cpu", "--confirm", "--wait"],
        upload={"id": "job", "status": "staged"},
        waits=[
            {"id": "job", "status": "staged"},
            {
                "id": "job",
                "status": "installing",
                "resources": [
                    {
                        "resource": "cpu",
                        "status": "failed",
                        "error": "Missing runtime profile",
                    }
                ],
            },
        ],
        confirmation={"id": "job", "status": "installing"},
    )

    with pytest.raises(SystemExit) as failure:
        cli.main()

    assert failure.value.code == 1
    assert calls == ["upload", "wait", "confirm", "wait"]
    assert "cpu: Missing runtime profile" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["status", "list"])
def test_main_status_and_list_keep_failed_installations_inspectable(
    monkeypatch, capsys, command
):
    result = {"id": "job", "status": "failed", "error": "Invalid return contract"}
    arguments = [command, "job"] if command == "status" else [command]
    calls = _main_client(monkeypatch, arguments, status=result)

    cli.main()

    assert calls == [command]
    output = json.loads(capsys.readouterr().out)
    assert (output[0] if command == "list" else output) == result


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/server",
        "https://user:password@example.org",
        "https://example.org?query=1",
        "https://example.org#fragment",
    ],
)
def test_cli_rejects_non_api_urls_and_embedded_credentials(url):
    with pytest.raises(ValueError):
        InstallationClient(url, "token")


def test_cli_does_not_follow_redirects_with_bearer_tokens():
    assert (
        _NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://elsewhere.test"
        )
        is None
    )
