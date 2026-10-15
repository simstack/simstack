"""Environment-backed configuration for the runner API transport."""

from __future__ import annotations

import os
import math
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
    def from_file(cls, config_path: str | os.PathLike[str]) -> "RunnerConnectionSettings":
        try:
            with Path(config_path).open("rb") as configuration_file:
                configuration = tomllib.load(configuration_file)
        except OSError:
            raise RunnerApiConfigurationError(
                "Runner connection configuration cannot be read"
            ) from None
        except tomllib.TOMLDecodeError:
            # Do not echo file contents: an operator may have supplied an old
            # secret-bearing file instead of the downloaded connection config.
            raise RunnerApiConfigurationError(
                "Runner connection configuration is not valid TOML"
            ) from None
        return cls.from_mapping(configuration)

    @classmethod
    def from_mapping(cls, configuration: Mapping[str, object]) -> "RunnerConnectionSettings":
        if not isinstance(configuration, Mapping) or set(configuration) != {"runner"}:
            raise RunnerApiConfigurationError(
                "Runner connection configuration must contain only a [runner] table; "
                "legacy database configuration is not supported"
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
            if not isinstance(value, str) or not value.strip() or value != value.strip():
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
                and not any(character.isspace() or ord(character) < 32 for character in server_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise RunnerApiConfigurationError(
                "Runner connection server_url must be an absolute HTTP(S) API URL "
                "without credentials, parameters, query, or fragment"
            )
        return cls(server_url=server_url, resource=values["resource"])


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
