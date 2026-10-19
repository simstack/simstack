from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from simstack.util.project_root_finder import find_project_root
from simstack.util.setup_logging import setup_logging
from simstack.util.mappings import ModelMappingTable, NodeMappingTable

if TYPE_CHECKING:
    from simstack.core.node_persistence import PersistentNodeExecutor
    from simstack.persistence import Repository
    from simstack.runner_runtime.execution_adapter import (
        RunnerExecutionAdapterConfig,
    )
    from simstack.util.resource_config import ResourceConfig


class GlobalState:
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._repository = None
            cls._instance._node_executor = None
            cls._instance._log_handler = None
            cls._instance._path_manager = None
            cls._instance._config = None
            cls._instance._model_mappings = None
            cls._instance._node_mappings = None
            cls._instance._resource_config = None
            cls._instance._runner_adapter = None
            cls._instance._runner_api = None
            cls._instance._runner_mode = False
            cls._instance._worker_mode = False
            cls._instance._runner_boundary = False
        return cls._instance

    def __init__(self, **kwargs):
        """Initialize GlobalState instance.

        This method only runs once due to the singleton pattern.
        Use ``initialize`` with an explicitly injected repository for server-side
        domain work, or ``initialize_runner`` for an API-only runner.
        """
        # All one-time state is created in ``__new__``.  Re-running ``__init__``
        # on the singleton must never reset the sticky API-runner boundary.

    def __getattribute__(self, name):
        # These special attributes should always be accessible
        if name in (
            "_initialized",
            "initialize",
            "initialize_runner",
            "initialized",
        ):
            return object.__getattribute__(self, name)

        # For other attributes, check initialization
        if not object.__getattribute__(self, "_initialized"):
            raise RuntimeError(
                "GlobalState must be initialized with initialize() or initialize_runner() before use"
            )

        return object.__getattribute__(self, name)

    async def initialize(
        self,
        *,
        repository: Repository,
        **kwargs,
    ) -> None:
        """Initialize non-runner state with an injected persistence port.

        The shared package never constructs a database client.  The server owns
        concrete persistence and passes its repository explicitly.
        """
        if object.__getattribute__(self, "_runner_boundary"):
            raise RuntimeError(
                "API-only runner context cannot be reinitialized with a repository"
            )
        is_test = kwargs.get("is_test", False)

        if self._initialized:
            if not is_test:
                return
        if repository is None:
            raise TypeError("GlobalState.initialize requires a repository")

        self._initialized = True
        self._runner_mode = False
        self._worker_mode = False
        self._runner_api = None
        self._runner_adapter = None
        self._repository = repository
        from simstack.core.node_persistence import PersistentNodeExecutor

        self._node_executor = PersistentNodeExecutor(repository)

        project_root = kwargs.get("project_root", find_project_root())
        if project_root is None:
            project_root = find_project_root()
        project_root = Path(project_root)
        kwargs["project_root"] = project_root
        self.initialize_logging(is_test, kwargs.get("log_level", "INFO"))

        if not kwargs.get("skip_config", False):
            await self.initialize_configs(
                repository,
                kwargs.get("toml_reader"),
                **kwargs,
            )

        await self.refresh_mappings()
        self._initialized = True

    async def initialize_runner(self, **kwargs):
        """Initialize the NodeRunner strictly from the server control plane.

        This path intentionally never receives a repository and never reads
        database credentials from ``simstack.toml``. Both server URL and runner
        token are mandatory; bootstrap failure aborts runner startup.
        """
        from simstack.core.resources import allowed_resources
        from simstack.models.document import decode_document_value
        from simstack.models.models import NodeModel
        from simstack.runner_api import (
            ExecutionRunnerClient,
            RunnerApiConfigurationError,
            SupervisorRunnerClient,
        )
        from simstack.runner_protocol import (
            FILE_COMPLETE_SCOPE,
            FILE_CREATE_SCOPE,
            FILE_DOWNLOAD_SCOPE,
            FILE_STATUS_SCOPE,
            FILE_UPLOAD_SCOPE,
            RUNNER_CURRENT_RESOURCE_ALIAS,
            RUNNER_EXECUTION_SCOPES,
            RUNNER_SUPERVISOR_SCOPES,
            RunnerModelMappingV1,
            RunnerProtocolError,
            validate_runner_mapping_tables_v1,
        )
        from simstack.runner_runtime.config import RunnerConfig
        from simstack.runner_runtime.execution_adapter import (
            RunnerExecutionAdapterConfig,
        )

        if self._initialized:
            if self._runner_mode:
                raise RuntimeError("Runner context is already initialized")
            raise RuntimeError(
                "API-only runner initialization refuses a repository-initialized context"
            )

        self._runner_boundary = True
        self._initialized = True
        self._runner_mode = True
        self._worker_mode = False
        self._repository = None
        self._node_executor = None
        try:
            from simstack.runner_runtime.environment import (
                assert_database_runtime_unavailable,
            )

            assert_database_runtime_unavailable()
            project_root = Path(
                kwargs.get("project_root") or find_project_root()
            ).resolve()
            task_id = kwargs.get("task_id")
            claim_id = kwargs.get("claim_id")
            attempt = kwargs.get("attempt")
            worker_binding = any(
                value is not None for value in (task_id, claim_id, attempt)
            )
            client_type = (
                ExecutionRunnerClient if worker_binding else SupervisorRunnerClient
            )
            client = client_type.from_environment(
                server_url=kwargs.get("server_url"),
                runner_token=kwargs.get("runner_token"),
            )
            if worker_binding:
                if not task_id or not claim_id or type(attempt) is not int:
                    raise RunnerApiConfigurationError(
                        "Worker bootstrap requires task_id, claim_id and attempt"
                    )
                if attempt < 1:
                    raise RunnerApiConfigurationError(
                        "Worker bootstrap attempt must be positive"
                    )
                bootstrap = await client.task_bootstrap(
                    str(task_id), claim_id=str(claim_id), attempt=attempt
                )
                self._worker_mode = True
            else:
                bootstrap = await client.bootstrap()
            granted_scopes = bootstrap.get("scopes")
            if not isinstance(granted_scopes, list) or not all(
                isinstance(scope, str) for scope in granted_scopes
            ):
                raise RunnerApiConfigurationError(
                    "Runner bootstrap scopes must be a list of names"
                )
            if worker_binding:
                required_scopes = set(RUNNER_EXECUTION_SCOPES)
                if not bootstrap.get("capabilities", {}).get("object_storage", False):
                    required_scopes.discard(FILE_UPLOAD_SCOPE)
                if not kwargs.get("with_file_transfer", True):
                    required_scopes.difference_update(
                        {
                            FILE_CREATE_SCOPE,
                            FILE_DOWNLOAD_SCOPE,
                            FILE_STATUS_SCOPE,
                            FILE_COMPLETE_SCOPE,
                        }
                    )
            else:
                required_scopes = set(RUNNER_SUPERVISOR_SCOPES)
                if not kwargs.get("with_file_transfer", True):
                    required_scopes.difference_update(
                        {FILE_UPLOAD_SCOPE, FILE_STATUS_SCOPE}
                    )
            missing_scopes = sorted(required_scopes.difference(granted_scopes))
            if missing_scopes:
                raise RunnerApiConfigurationError(
                    "Runner token is missing required scopes: "
                    + ", ".join(missing_scopes)
                )
            resource_payload = bootstrap.get("resource")
            if not isinstance(resource_payload, dict):
                raise RunnerApiConfigurationError(
                    "Runner bootstrap resource must be an object"
                )

            bootstrap_resource = str(resource_payload.get("resource_str") or "")
            requested_resource = str(kwargs.get("resource") or bootstrap_resource)
            if not bootstrap_resource:
                raise RunnerApiConfigurationError(
                    "Runner bootstrap did not contain resource.resource_str"
                )
            if requested_resource != bootstrap_resource:
                raise RunnerApiConfigurationError(
                    "Runner token resource mismatch: "
                    f"requested {requested_resource!r}, server returned {bootstrap_resource!r}"
                )

            configured_resources = bootstrap.get("allowed_resources")
            if not isinstance(configured_resources, list) or not all(
                isinstance(value, str) and value for value in configured_resources
            ):
                raise RunnerApiConfigurationError(
                    "Runner bootstrap allowed_resources must be a list of names"
                )
            resource_names = list(dict.fromkeys(configured_resources))
            if bootstrap_resource not in resource_names:
                raise RunnerApiConfigurationError(
                    "Runner bootstrap resource is absent from allowed_resources"
                )
            if RUNNER_CURRENT_RESOURCE_ALIAS not in resource_names:
                resource_names.append(RUNNER_CURRENT_RESOURCE_ALIAS)
            allowed_resources.set_resources(resource_names)

            self._runner_api = client
            self._config = RunnerConfig.from_bootstrap(
                bootstrap,
                project_root=project_root,
                server_url=client.server_url,
                workdir_override=(
                    Path(str(workdir_override))
                    if (
                        workdir_override := (
                            kwargs.get("workdir_override")
                            or os.environ.get("SIMSTACK_RUNNER_WORKDIR")
                        )
                    )
                    else None
                ),
            )
            if not self._config.workdir.is_dir():
                raise RunnerApiConfigurationError(
                    f"Runner workdir does not exist: {self._config.workdir}"
                )
            from simstack.runner_runtime.import_paths import (
                configure_runner_import_paths,
            )

            configure_runner_import_paths(self._config)
            if worker_binding and bootstrap.get("workflow_packages"):
                from simstack.runner_runtime.workflow_installation import configure_package_imports

                configure_package_imports(bootstrap["workflow_packages"], workdir=self._config.workdir)
            # Declared workflow roots must not make database drivers available
            # after the initial runtime check, including in nested workers.
            assert_database_runtime_unavailable()

            if worker_binding:
                # Worker processes consume only the claim-scoped API bootstrap
                # and the explicit allowlist propagated by their supervisor.
                # Operator launch commands/configuration never cross into the
                # untrusted workflow process.
                self._runner_adapter = None
            else:
                resource_config_file = kwargs.get("config_file")
                if resource_config_file is None:
                    resource_config_file = project_root / "config.toml"
                self._runner_adapter = RunnerExecutionAdapterConfig.from_file(
                    Path(resource_config_file), bootstrap_resource
                )
            self._resource_config = None

            try:
                model_payloads, node_payloads = validate_runner_mapping_tables_v1(
                    bootstrap.get("model_mappings"),
                    bootstrap.get("node_models"),
                    operation="bootstrap",
                )
            except RunnerProtocolError as exc:
                raise RunnerApiConfigurationError(str(exc)) from exc

            model_mappings = []
            model_names: set[str] = set()
            model_paths: set[str] = set()
            for raw_mapping in model_payloads:
                try:
                    model_mapping = RunnerModelMappingV1.model_validate(raw_mapping)
                except (TypeError, ValueError) as exc:
                    raise RunnerApiConfigurationError(
                        "Runner bootstrap contains an invalid model mapping"
                    ) from exc
                if (
                    model_mapping.name in model_names
                    or model_mapping.mapping in model_paths
                ):
                    raise RunnerApiConfigurationError(
                        "Runner bootstrap contains duplicate model mappings"
                    )
                model_names.add(model_mapping.name)
                model_paths.add(model_mapping.mapping)
                model_mappings.append(model_mapping)

            node_mappings = []
            node_names: set[str] = set()
            node_paths: set[str] = set()
            for raw_node in node_payloads:
                node = decode_document_value(dict(raw_node))
                try:
                    node_mapping = NodeModel.model_validate(node)
                except (TypeError, ValueError) as exc:
                    raise RunnerApiConfigurationError(
                        "Runner bootstrap contains an invalid node model"
                    ) from exc
                if (
                    node_mapping.name in node_names
                    or node_mapping.function_mapping in node_paths
                ):
                    raise RunnerApiConfigurationError(
                        "Runner bootstrap contains duplicate node mappings"
                    )
                node_names.add(node_mapping.name)
                node_paths.add(node_mapping.function_mapping)
                node_mappings.append(node_mapping)

            self._model_mappings = ModelMappingTable(model_mappings)
            self._node_mappings = NodeMappingTable(node_mappings)
            self.initialize_logging(
                is_test=False,
                log_level=kwargs.get("log_level", "INFO"),
                runner_api=client,
                runner_task_id=str(task_id) if worker_binding else None,
            )
            logging.getLogger("Context").info(
                "Runner API connection to %s for resource %s",
                client.server_url,
                bootstrap_resource,
            )
        except Exception:
            self._runner_api = None
            self._worker_mode = False
            self._config = None
            self._resource_config = None
            self._runner_adapter = None
            self._model_mappings = None
            self._node_mappings = None
            self._node_executor = None
            self._initialized = False
            raise

    async def initialize_configs(self, repository: Repository, toml_reader, **kwargs):
        project_root = kwargs["project_root"]
        resource_str = kwargs.get("resource", "self")
        is_test = kwargs.get("is_test", False)
        try:
            from simstack.util.config_reader import ConfigReader
            from simstack.util.resource_config import ResourceConfig

            resource_config_file = kwargs.get("config_file")
            if resource_config_file is None:
                resource_config_file = project_root / "config.toml"
            else:
                resource_config_file = Path(resource_config_file)
            self._config = await ConfigReader.create(
                resource_str,
                repository,
                toml_reader,
                **kwargs,
            )
            self._resource_config = ResourceConfig(resource_config_file, resource_str)
            self._runner_adapter = None
        except Exception as e:
            if is_test:
                logger = logging.getLogger("Context")
                logger.warning(f"Failed to initialize ConfigReader in test mode: {e}")
            else:
                raise e

    async def refresh_mappings(self, *, models: bool = True, nodes: bool = True):
        from simstack.application.mapping_registry import (
            load_model_mapping_table,
            load_node_mapping_table,
        )

        if models:
            self._model_mappings = await load_model_mapping_table(self.repository)
        if nodes:
            self._node_mappings = await load_node_mapping_table(self.repository)

    def initialize_logging(
        self,
        is_test: bool,
        log_level: str = "INFO",
        runner_api=None,
        runner_task_id: str | None = None,
    ):
        if is_test:
            # For tests, use simple console logging without the database handler
            # We use force=True to ensure it overrides any existing configuration (e.g. from pytest or PyCharm)
            # We also ensure the stream is sys.stderr so PyCharm/pytest can capture it properly if configured
            logging.basicConfig(
                level=log_level,
                format="%(asctime)s - %(name)-15s - %(levelname)-10s - %(filename)-20s:%(lineno)4d - %(message)s",
                stream=sys.stderr,
                force=True,
            )
            self._log_handler = logging.getLogger()
            # Flush stdout and stderr to ensure logs are not buffered
            sys.stdout.flush()
            sys.stderr.flush()
        else:
            self._log_handler = setup_logging(
                log_level,
                runner_api=runner_api,
                runner_task_id=runner_task_id,
            )

    @property
    def repository(self) -> Repository:
        if self._runner_mode:
            raise RuntimeError("Persistence is forbidden in API-only runner mode")
        if self._repository is None:
            raise RuntimeError("No repository was injected")
        return self._repository

    @property
    def node_executor(self) -> "PersistentNodeExecutor":
        if self._runner_mode:
            raise RuntimeError(
                "Persistent node execution is forbidden in API-only runner mode"
            )
        if self._node_executor is None:
            raise RuntimeError("No persistent node executor was injected")
        return self._node_executor

    @property
    def runner_api(self):
        if not self._runner_mode or self._runner_api is None:
            raise RuntimeError("Runner API is only available in API-only runner mode")
        return self._runner_api

    @property
    def runner_mode(self) -> bool:
        return self._runner_mode

    @property
    def worker_mode(self) -> bool:
        return self._worker_mode

    @property
    def config(self):
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
    def resource_config(self) -> "ResourceConfig":
        if self._runner_mode:
            raise RuntimeError(
                "Legacy ResourceConfig is unavailable in API-only runner mode"
            )
        return self._resource_config

    @property
    def runner_adapter(self) -> "RunnerExecutionAdapterConfig":
        if not self._runner_mode or self._runner_adapter is None:
            raise RuntimeError(
                "Runner execution adapter is only available to the API-only supervisor"
            )
        return self._runner_adapter

    @property
    def initialized(self):
        return self._initialized


# Create the singleton instance, but it's not initialized yet
context = GlobalState()
