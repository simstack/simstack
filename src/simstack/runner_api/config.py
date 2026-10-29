"""Environment-backed configuration for the runner API transport."""

from __future__ import annotations

import json
import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import RunnerApiConfigurationError

SERVER_URL_ENV = "SIMSTACK_SERVER_URL"
RUNNER_TOKEN_ENV = "SIMSTACK_RUNNER_TOKEN"
REQUEST_TIMEOUT_ENV = "SIMSTACK_RUNNER_REQUEST_TIMEOUT_SECONDS"
FILE_TRANSFER_REQUEST_TIMEOUT_ENV = "SIMSTACK_FILE_TRANSFER_REQUEST_TIMEOUT_SECONDS"
FILE_TRANSFER_TIMEOUT_ENV = "SIMSTACK_FILE_TRANSFER_TIMEOUT_SECONDS"
FILE_TRANSFER_POLL_ENV = "SIMSTACK_FILE_TRANSFER_POLL_SECONDS"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.1
DEFAULT_FILE_TRANSFER_TIMEOUT_SECONDS = 3600
DEFAULT_FILE_TRANSFER_POLL_SECONDS = 5.0


@dataclass(frozen=True)
class RunnerConnectionSettings:
    """Secret-free connection facts, separate from local worker launch policy."""

    server_url: str
    resource: str

    @classmethod
    def from_file(
        cls, config_path: str | os.PathLike[str]
    ) -> "RunnerConnectionSettings":
        return cls.from_mapping(_load_runner_configuration(config_path))

    @classmethod
    def from_execution_file(
        cls, config_path: str | os.PathLike[str]
    ) -> "RunnerConnectionSettings | None":
        """Read unified connection settings, or retain an explicit legacy adapter.

        Files without a runner table contain only operator execution settings;
        their API credentials continue to come from the environment. A supplied
        runner table is authoritative and is never silently ignored.
        """
        configuration = _load_runner_configuration(config_path)
        if "runner" not in configuration:
            return None
        # A legacy execution-only file may name its resource "runner".
        raw_runner = configuration["runner"]
        legacy_keys = {"program", "setup", "workflow_profiles"}
        if (
            isinstance(raw_runner, Mapping)
            and raw_runner
            and set(raw_runner).issubset(legacy_keys)
        ):
            return None
        return cls.from_mapping(configuration)

    @classmethod
    def from_mapping(
        cls, configuration: Mapping[str, object]
    ) -> "RunnerConnectionSettings":
        if (
            not isinstance(configuration, Mapping)
            or "runner" not in configuration
            or not set(configuration).issubset({"runner", "execution"})
        ):
            raise RunnerApiConfigurationError(
                "Runner configuration may contain only [runner] and [execution] tables; "
                "legacy database configuration is not supported"
            )
        if "execution" in configuration and not isinstance(
            configuration["execution"], Mapping
        ):
            raise RunnerApiConfigurationError(
                "Runner execution configuration must be a table"
            )
        runner = configuration["runner"]
        if not isinstance(runner, Mapping) or set(runner) != {"server_url", "resource"}:
            raise RunnerApiConfigurationError(
                "The [runner] table must contain only server_url and resource; "
                "keep the runner token in SIMSTACK_RUNNER_TOKEN"
            )
        values = {}
        for name in ("server_url", "resource"):
            value = runner[name]
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise RunnerApiConfigurationError(
                    f"Runner connection {name} must be a non-empty, trimmed string"
                )
            values[name] = value
        server_url = values["server_url"]
        try:
            parsed = urlparse(server_url)
            valid_url = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.port != 0
                and parsed.username is None
                and parsed.password is None
                and not parsed.params
                and not parsed.query
                and not parsed.fragment
                and not any(
                    character.isspace() or ord(character) < 32
                    for character in server_url
                )
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise RunnerApiConfigurationError(
                "Runner connection server_url must be an absolute HTTP(S) API URL "
                "without credentials, parameters, query, or fragment"
            )
        return cls(server_url=server_url, resource=values["resource"])


def _load_runner_configuration(config_path: str | os.PathLike[str]) -> dict:
    try:
        path = Path(config_path)
        if path.is_dir():
            path = path / "config.toml"
        with path.open("rb") as configuration_file:
            return tomllib.load(configuration_file)
    except OSError:
        raise RunnerApiConfigurationError(
            "Runner configuration cannot be read"
        ) from None
    except tomllib.TOMLDecodeError:
        # Do not include the source: a mistakenly supplied old file may contain secrets.
        raise RunnerApiConfigurationError(
            "Runner configuration is not valid TOML"
        ) from None


def render_runner_config(server_url: str, resource: str) -> str:
    """Return one secret-free config with portable basic local Python defaults.

    The defaults run under the current user's API-only interpreter. They do not
    add an OS network sandbox; operators may strengthen the worker boundary or
    add explicit cluster/container profiles without changing the connection.
    """
    connection = RunnerConnectionSettings.from_mapping(
        {"runner": {"server_url": server_url, "resource": resource}}
    )
    return "\n".join(
        (
            "# SimStack API Runner configuration. Keep SIMSTACK_RUNNER_TOKEN in the environment.",
            "# Basic local Python execution under the current user; no OS network sandbox is added.",
            "# Customize execution settings here for cluster/container resources.",
            "[runner]",
            f"server_url = {json.dumps(connection.server_url, ensure_ascii=False)}",
            f"resource = {json.dumps(connection.resource, ensure_ascii=False)}",
            "",
            "[execution.program.python]",
            'local_worker_command = ["{runner_python}", "-m", "simstack.core.run_node"]',
            'worker_network_guard_command = ["/usr/bin/env"]',
            "worker_environment_allowlist = []",
            "",
            "[execution.workflow_profiles.python]",
            'installer_python = "{runner_python}"',
            'local_worker_command = ["{python}", "-m", "simstack.core.run_node"]',
            'worker_network_guard_command = ["/usr/bin/env"]',
            "worker_environment_allowlist = []",
            "",
        )
    )


@dataclass(frozen=True)
class RunnerApiSettings:
    server_url: str
    runner_token: str
    timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS

    @classmethod
    def from_environment(
        cls,
        *,
        server_url: str | None = None,
        runner_token: str | None = None,
    ) -> "RunnerApiSettings":
        resolved_url = (
            server_url if server_url is not None else os.environ.get(SERVER_URL_ENV)
        )
        resolved_token = (
            runner_token
            if runner_token is not None
            else os.environ.get(RUNNER_TOKEN_ENV)
        )
        missing = []
        if not isinstance(resolved_url, str) or not resolved_url.strip():
            missing.append(SERVER_URL_ENV)
        if not isinstance(resolved_token, str) or not resolved_token.strip():
            missing.append(RUNNER_TOKEN_ENV)
        if missing:
            raise RunnerApiConfigurationError(
                "API-only runner requires " + " and ".join(missing)
            )

        raw_timeout = os.environ.get(
            REQUEST_TIMEOUT_ENV, str(DEFAULT_REQUEST_TIMEOUT_SECONDS)
        )
        try:
            timeout_seconds = int(raw_timeout)
        except ValueError as exc:
            raise RunnerApiConfigurationError(
                f"{REQUEST_TIMEOUT_ENV} must be an integer"
            ) from exc
        if timeout_seconds <= 0:
            raise RunnerApiConfigurationError(f"{REQUEST_TIMEOUT_ENV} must be positive")
        return cls(
            server_url=resolved_url.strip(),
            runner_token=resolved_token.strip(),
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class FileTransferApiSettings:
    request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS
    transfer_timeout_seconds: int = DEFAULT_FILE_TRANSFER_TIMEOUT_SECONDS
    poll_seconds: float = DEFAULT_FILE_TRANSFER_POLL_SECONDS

    @classmethod
    def from_environment(cls) -> "FileTransferApiSettings":
        request_timeout = _positive_int_environment(
            FILE_TRANSFER_REQUEST_TIMEOUT_ENV,
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        transfer_timeout = _positive_int_environment(
            FILE_TRANSFER_TIMEOUT_ENV,
            DEFAULT_FILE_TRANSFER_TIMEOUT_SECONDS,
        )
        raw_poll = os.environ.get(
            FILE_TRANSFER_POLL_ENV,
            str(DEFAULT_FILE_TRANSFER_POLL_SECONDS),
        )
        try:
            poll_seconds = float(raw_poll)
        except ValueError as exc:
            raise RunnerApiConfigurationError(
                f"{FILE_TRANSFER_POLL_ENV} must be numeric"
            ) from exc
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise RunnerApiConfigurationError(
                f"{FILE_TRANSFER_POLL_ENV} must be finite and positive"
            )
        return cls(
            request_timeout_seconds=request_timeout,
            transfer_timeout_seconds=transfer_timeout,
            poll_seconds=poll_seconds,
        )


def _positive_int_environment(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RunnerApiConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RunnerApiConfigurationError(f"{name} must be positive")
    return value
