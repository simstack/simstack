from simstack.server_api import ServerAPI
from simstack.server_api.http_transport import RunnerAPIClient
from simstack.util.object_transfer import ObjectFileClient

object_client_for_api = ObjectFileClient.for_api
object_client_from_context = ObjectFileClient.from_context


def test_user_object_client_keeps_explicit_user_credentials_and_user_routes(monkeypatch):
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "ambient-runner-token")
    monkeypatch.setattr("simstack.runner_runtime.runner_credentials.current_token", lambda *args: "rotated-runner-token")
    user = ServerAPI.for_user("https://simstack.test/api", "explicit-user-token")
    client = object_client_for_api(user.requests)
    assert client.object_api_prefix == "/files/objects"
    assert client.runner_token == "explicit-user-token"
    assert client.rotate_credentials is False
    assert client._auth_headers() == {"Authorization": "Bearer explicit-user-token"}
    from simstack.core.context import context
    monkeypatch.setattr(context, "_server", user)
    monkeypatch.setattr(ObjectFileClient, "for_api", object_client_for_api)
    configured = object_client_from_context()
    assert configured.object_api_prefix == "/files/objects"
    assert configured._auth_headers() == {"Authorization": "Bearer explicit-user-token"}


def test_runner_object_client_retains_runner_scope_and_token_rotation(monkeypatch):
    monkeypatch.setattr("simstack.runner_runtime.runner_credentials.current_token", lambda *args: "rotated-runner-token")
    runner = ServerAPI(RunnerAPIClient("https://simstack.test/api", "initial-runner-token"))
    client = object_client_for_api(runner.requests)
    assert client.object_api_prefix == "/runner/objects"
    assert client.runner_token == "rotated-runner-token"
    assert client.rotate_credentials is True


def test_local_execution_object_client_keeps_scoped_credentials_and_routes(monkeypatch):
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "ambient-runner-token")
    monkeypatch.setattr("simstack.runner_runtime.runner_credentials.current_token", lambda *args: "rotated-runner-token")
    local = ServerAPI.for_local_execution("https://simstack.test/api", "task-scoped-token")
    client = object_client_for_api(local.requests)
    assert client.object_api_prefix == "/local-execution/v3/objects"
    assert client.runner_token == "task-scoped-token"
    assert client.rotate_credentials is False
    assert client._auth_headers() == {"Authorization": "Bearer task-scoped-token"}
