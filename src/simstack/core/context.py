"""Execution configuration and explicit domain clients; no persistence backend."""

from __future__ import annotations
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from simstack.util.project_root_finder import find_project_root

_execution_server = ContextVar("simstack_execution_server", default=None)


@contextmanager
def execution_server(server):
    """Keep a local root credential within its asynchronous invocation."""
    if server is None:
        yield
        return
    token = _execution_server.set(server)
    try:
        yield
    finally:
        _execution_server.reset(token)


class GlobalState:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if "_initialized" in vars(self):
            return
        self._initialized = False
        self._server = None
        self._config = None
        self._resource_config = None
        self._model_mappings = None
        self._node_mappings = None
        self._in_docker = False
        self._current_node_name = None
        self._log_handler = None

    async def initialize(self, **kwargs):
        if any(
            key in kwargs
            for key in ("db_name", "db_type", "connection_string", "database")
        ):
            raise ValueError(
                "Execution contexts accept API credentials, not database configuration"
            )
        if self._initialized and not kwargs.get("is_test", False):
            return
        from simstack.server_api import ServerAPI
        from simstack.server_api.http_transport import (
            RunnerAPIClient,
            runner_environment,
        )
        from simstack.runner_runtime.runner_credentials import update_known_hosts
        from simstack.server_api.log_upload import setup_runner_logging
        from simstack.core.resources import allowed_resources
        from simstack.models.resource_definition import ResourceDefinition
        from simstack.util.config_reader import ConfigReader
        from simstack.util.resource_config import ResourceConfig
        from simstack.util.transform_file_name import transform_file_name

        self._initialized = False
        project_root = (
            Path(kwargs.get("project_root") or find_project_root())
            .expanduser()
            .resolve()
        )
        server_url = kwargs.get("server_url") or os.environ.get("SIMSTACK_SERVER_URL")
        token = (
            kwargs.get("server_token")
            or os.environ.get("SIMSTACK_RUNNER_TOKEN")
            or os.environ.get("SIMSTACK_SERVER_TOKEN")
        )
        settings_file = project_root / "simstack.toml"
        if settings_file.is_file() and (not server_url or not token):
            import tomllib

            with settings_file.open("rb") as source:
                server_settings = (
                    tomllib.load(source).get("parameters", {}).get("server", {})
                )
            server_url = server_url or server_settings.get("url")
            token = token or server_settings.get("token")
        user_token = kwargs.get("user_token") or (
            os.environ.get("SIMSTACK_USER_TOKEN")
            if not (kwargs.get("server_token") or os.environ.get("SIMSTACK_RUNNER_TOKEN") or os.environ.get("SIMSTACK_SERVER_TOKEN"))
            else None
        )
        if user_token and kwargs.get("server_token"):
            raise ValueError("Choose either user or Runner authorization")
        server = kwargs.get("server")
        if server is None:
            local_token = os.environ.get("SIMSTACK_LOCAL_EXECUTION_TOKEN")
            if local_token:
                if user_token or kwargs.get("server_token"):
                    raise ValueError("Choose one execution authorization")
                server = ServerAPI.for_local_execution(server_url, local_token)
            elif user_token:
                server = ServerAPI.for_user(
                    server_url, user_token, resource=kwargs.get("resource", "self")
                )
            else:
                server = ServerAPI(RunnerAPIClient(server_url, token))
        bootstrap = await server.resource.bootstrap()
        account_id = os.environ.get("SIMSTACK_EXECUTION_ACCOUNT_ID")
        if account_id and account_id != bootstrap.get("account_id"):
            raise ValueError("Execution credentials belong to a different account")
        resource_name = bootstrap["resource"]
        requested = kwargs.get("resource", "self")
        if requested not in {"self", "local", resource_name}:
            raise ValueError("Runner token is bound to a different resource")
        from simstack.models.document.codec import decode_document_value

        resource = ResourceDefinition.model_validate(
            decode_document_value(bootstrap["resource_definition"])
        )
        allowed_resources.set_resources(
            list(dict.fromkeys([*bootstrap["resources"], "self"]))
        )
        update_known_hosts(bootstrap.get("known_hosts"))
        user_mode = server.requests.authorization in {"user", "local"}
        workdir = kwargs.get("workdir") or (
            project_root / "work"
            if user_mode
            else os.environ.get("SIMSTACK_RUNNER_WORKDIR")
            or resource.workdir
            or project_root / "work"
        )
        resolved = (
            transform_file_name(workdir, project_root, require_exists=False)
            .expanduser()
            .resolve()
        )
        resolved.mkdir(parents=True, exist_ok=True)
        resource.workdir = str(resolved)
        for key in set(os.environ) - set(runner_environment()):
            os.environ.pop(key, None)
        if not user_mode:
            os.environ["SIMSTACK_SERVER_URL"] = server.requests.server_url
            os.environ["SIMSTACK_RUNNER_TOKEN"] = server.requests.token
        os.environ["SIMSTACK_RUNNER_WORKDIR"] = str(resolved)
        if kwargs.get("config_file"):
            os.environ["SIMSTACK_MACHINE_CONFIG"] = str(
                Path(kwargs["config_file"]).expanduser().resolve()
            )
        config_file = (
            kwargs.get("config_file")
            or os.environ.get("SIMSTACK_MACHINE_CONFIG")
            or project_root / "config.toml"
        )
        self._server = server
        self._config = ConfigReader(
            resource,
            project_root=project_root,
            server_url=server.requests.server_url,
            server_token=server.requests.token,
            account_id=bootstrap["account_id"],
        )
        self._resource_config = ResourceConfig(Path(config_file), resource_name)
        self._in_docker = bool(kwargs.get("in_docker", False))
        self._current_node_name = None
        if user_mode:
            from simstack.runner_runtime.launch_claims import current_execution
            execution = current_execution()
            if execution:
                self._log_handler = setup_runner_logging(server.requests.transport,
                    kwargs.get("log_level", "INFO"), execution=execution, resource=resource_name,
                    execution_prefix=server.requests.prefix)
            else:
                import logging
                logging.basicConfig(level=kwargs.get("log_level", "INFO"))
                self._log_handler = logging.getLogger()
        else:
            self._log_handler = setup_runner_logging(
                server.requests.transport, kwargs.get("log_level", "INFO")
            )
        if bootstrap.get("repository_reconciliation_required"):
            from simstack.util.repository_runtime import reconcile_prepared_repositories

            await reconcile_prepared_repositories(server.resource)
        if kwargs.get("load_catalogue", True):
            await self.refresh_mappings()
        self._initialized = True

    async def refresh_mappings(self, *, models=True, nodes=True):
        from simstack.util.mappings import ModelMappingTable, NodeMappingTable

        if models:
            self._model_mappings = ModelMappingTable(
                await self._server.values.catalogue_models()
            )
        if nodes:
            self._node_mappings = NodeMappingTable(
                await self._server.values.catalogue_nodes()
            )

    @property
    def initialized(self):
        return self._initialized

    @property
    def server(self):
        scoped = _execution_server.get()
        if scoped is not None:
            return scoped
        if self._server is None:
            raise RuntimeError(
                "Initialize the execution context with API credentials first"
            )
        return self._server

    @property
    def config(self):
        if self._config is None:
            raise RuntimeError("Initialize the execution context first")
        return self._config

    @config.setter
    def config(self, value):
        self._config = value

    @property
    def model_mappings(self):
        return self._model_mappings

    @model_mappings.setter
    def model_mappings(self, value):
        self._model_mappings = value

    @property
    def node_mappings(self):
        return self._node_mappings

    @node_mappings.setter
    def node_mappings(self, value):
        self._node_mappings = value

    @property
    def resource_config(self):
        return self._resource_config

    @property
    def in_docker(self):
        return self._in_docker

    @in_docker.setter
    def in_docker(self, value):
        self._in_docker = bool(value)

    @property
    def current_node_name(self):
        return self._current_node_name

    @current_node_name.setter
    def current_node_name(self, value):
        self._current_node_name = value


context = GlobalState()
