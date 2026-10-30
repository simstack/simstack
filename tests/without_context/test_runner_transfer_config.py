from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from simstack.core.resources import allowed_resources
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.config_reader import ConfigReader
from simstack.util.database_information import DatabaseInformation
from simstack.util.db import Database
from simstack.util.file_transfer_client import FileTransferClient
from simstack.util.toml_reader import TomlReader


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "use_db,config_token,token_env,has_token,expected_token",
    [
        (True, None, None, True, "local-token"),
        (True, "config-token", None, True, "config-token"),
        (True, None, "SIMSTACK_RUNNER_TOKEN", True, "env-token"),
        (True, None, "SIMSTACK_SERVER_TOKEN", True, "env-token"),
        (True, "config-token", "SIMSTACK_RUNNER_TOKEN", True, "env-token"),
        (False, None, None, True, None),
        (True, None, None, False, None),
    ],
)
async def test_runner_transfer_credentials(
    tmp_path,
    monkeypatch,
    caplog,
    use_db,
    config_token,
    token_env,
    has_token,
    expected_token,
):
    for name in (
        "SIMSTACK_RUNNER_TOKEN",
        "SIMSTACK_SERVER_TOKEN",
        "SIMSTACK_SERVER_URL",
        "SIMSTACK_DB_DATABASE",
        "SIMSTACK_DB_TEST_DATABASE",
        "SIMSTACK_DB_CONNECTION_STRING",
    ):
        monkeypatch.delenv(name, raising=False)
    if token_env:
        monkeypatch.setenv(token_env, "env-token")
    monkeypatch.setattr(allowed_resources, "_resources", [])
    monkeypatch.setattr(allowed_resources, "_initialized", False)
    (tmp_path / "simstack.toml").write_text(
        f"[parameters.general]\nuse_db = {str(use_db).lower()}\n"
        '[parameters.db]\ndatabase = "runner_test"\n'
        '[parameters.server]\nurl = "https://simstack.example/api"\n'
        + (f'token = "{config_token}"\n' if config_token else "")
    )
    toml = TomlReader(tmp_path)
    db = Database.from_db_info(
        DatabaseInformation.from_config(toml.config, is_test=True)
    )
    resource = ResourceDefinition(
        resource_str="local", hostname="localhost", workdir=str(tmp_path)
    )
    await db.collection(ResourceDefinition).insert_one(resource.model_dump_doc())
    tokens = db.collection("runner_resource_tokens")
    await tokens.insert_one(
        {"resource_str": "other", "access_token": "other-resource-token"}
    )
    await db.client["other_user"]["runner_resource_tokens"].insert_one(
        {"resource_str": "local", "access_token": "other-user-token"}
    )
    if has_token:
        await tokens.insert_one(
            {"resource_str": "local", "access_token": "local-token"}
        )

    collections = Mock(wraps=db.collection)
    monkeypatch.setattr(db, "collection", collections)
    try:
        config = await ConfigReader.create("local", db, toml, tmp_path)
        assert (call("runner_resource_tokens") in collections.call_args_list) == (
            use_db and not config_token and not token_env
        )
        monkeypatch.setattr(
            "simstack.core.context.context", SimpleNamespace(config=config)
        )
        client = FileTransferClient.from_context(required=False)

        if expected_token is None:
            assert client is None
        else:
            assert client is not None
            assert client.runner_token == expected_token
            assert client.server_url == "https://simstack.example/api"
            assert expected_token not in caplog.text
        assert "runner_access_token" not in config._resource_definition.model_dump()
    finally:
        await db.close()
