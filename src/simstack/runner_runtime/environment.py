"""Fail-closed process environment boundaries for runner processes."""

from __future__ import annotations

import importlib.util
import json
import os
import re
from collections.abc import Mapping, MutableMapping


RUNNER_CONTROL_ENVIRONMENT = (
    "SIMSTACK_SERVER_URL",
    "SIMSTACK_RUNNER_TOKEN",
    "SIMSTACK_TASK_ID",
    "SIMSTACK_TASK_CLAIM_ID",
    "SIMSTACK_TASK_ATTEMPT",
    "SIMSTACK_RUNNER_RESOURCE",
    "SIMSTACK_PROJECT_ROOT",
    "SIMSTACK_RUNNER_WORKDIR",
    "SIMSTACK_WORKER_ENVIRONMENT_ALLOWLIST",
)

RUNNER_RUNTIME_ENVIRONMENT = (
    "SIMSTACK_RUNNER_REQUEST_TIMEOUT_SECONDS",
    "SIMSTACK_RUNNER_LEASE_SECONDS",
    "SIMSTACK_RUNNER_CHILD_POLL_SECONDS",
    "SIMSTACK_FILE_TRANSFER_REQUEST_TIMEOUT_SECONDS",
    "SIMSTACK_FILE_TRANSFER_TIMEOUT_SECONDS",
    "SIMSTACK_FILE_TRANSFER_POLL_SECONDS",
)

SUPERVISOR_CONTROL_ENVIRONMENT = (
    "SIMSTACK_SERVER_URL",
    "SIMSTACK_RUNNER_TOKEN",
    *RUNNER_RUNTIME_ENVIRONMENT,
)

_BLOCKED_EXACT = {
    "CONNECTION_STRING",
    "DATABASE_URL",
    "DATABASE_URI",
    "DB_NAME",
    "SIMSTACK_CONFIG",
    "SIMSTACK_CONFIG_FILE",
    "SIMSTACK_CONFIG_PATH",
    "SIMSTACK_SERVER_TOKEN",
    "SIMSTACK_TOML",
}
_BLOCKED_PREFIXES = (
    "MONGO",
    "MONGODB",
    "SIMSTACK_DB",
    "SIMSTACK_TEST_DB",
    "DATABASE_",
    "DB_",
)
_BLOCKED_SUFFIXES = (
    "_CONNECTION_STRING",
    "_DATABASE_URL",
    "_DATABASE_URI",
    "_DB_URL",
    "_DB_URI",
)
_CONTAINER_ENV_PREFIXES = ("APPTAINERENV_", "SINGULARITYENV_")
_DATABASE_RUNTIME_PACKAGES = ("bson", "odmantic", "motor", "pymongo")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_WORKER_ENVIRONMENT = {
    "LANG",
    "LOGNAME",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
}
_SAFE_HELPER_ENVIRONMENT = _SAFE_WORKER_ENVIRONMENT | {
    "HOME",
    "SSH_AUTH_SOCK",
}


def _normalized_environment_name(name: str) -> str:
    normalized = name.strip().upper()
    for prefix in _CONTAINER_ENV_PREFIXES:
        if normalized.startswith(prefix):
            return normalized.removeprefix(prefix)
    return normalized


def is_blocked_runner_environment(name: str) -> bool:
    """Return whether an inherited variable could reconnect a worker to a DB."""

    normalized = _normalized_environment_name(name)
    if normalized in _BLOCKED_EXACT:
        return True
    if normalized.startswith(_BLOCKED_PREFIXES):
        return True
    if normalized.endswith(_BLOCKED_SUFFIXES):
        return True
    return normalized.startswith("SIMSTACK_") and (
        "CONFIG" in normalized or "TOML" in normalized
    )


def sanitized_runner_environment(
    control_environment: Mapping[str, str],
    *,
    allowed_environment_names: tuple[str, ...] = (),
    require_allowed: bool = True,
    source: Mapping[str, str] | None = None,
    include_runtime_controls: bool = True,
    workflow_bootstrap: bool = False,
) -> dict[str, str]:
    """Build a worker environment with DB/config secrets removed fail-closed."""

    inherited = os.environ if source is None else source
    control_names = {name.upper() for name in RUNNER_CONTROL_ENVIRONMENT}
    sanitized = {
        str(name): str(value)
        for name, value in inherited.items()
        if (
            str(name).upper() in _SAFE_WORKER_ENVIRONMENT
            or str(name).upper().startswith("LC_")
            or (
                include_runtime_controls
                and str(name).upper() in RUNNER_RUNTIME_ENVIRONMENT
            )
        )
        and str(name).upper() not in control_names
    }
    for name in allowed_environment_names:
        if name in inherited:
            sanitized[name] = str(inherited[name])
        elif require_allowed:
            raise ValueError(
                f"Required worker environment variable {name} is unavailable"
            )
    for name, value in control_environment.items():
        canonical_name = str(name).upper()
        if canonical_name not in control_names:
            raise ValueError(f"Unsupported runner control environment: {name}")
        if value is None or not str(value).strip():
            raise ValueError(f"Runner control environment {canonical_name} is required")
        sanitized[canonical_name] = str(value)
    if workflow_bootstrap:
        # Private dependency startup must decode .pth paths exactly as the
        # pre-launch filesystem guard. Keep ordinary program locale unchanged.
        sanitized.update(LANG="C.UTF-8", LC_ALL="C.UTF-8")
    return sanitized


def sanitize_runner_process_environment(
    *,
    allowed_environment_names: tuple[str, ...] = (),
    environment: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Replace the runner process environment with an explicit allowlist.

    This mutates ``os.environ`` by default. Unknown variables are removed even
    when their names do not resemble database credentials. Explicitly preserved
    workflow variables must pass the same validation as child-worker variables.
    """

    target = os.environ if environment is None else environment
    explicit_names = validate_worker_environment_allowlist(
        list(allowed_environment_names)
    )
    inherited = dict(target)
    allowed_names = {
        *_SAFE_HELPER_ENVIRONMENT,
        *(name.upper() for name in SUPERVISOR_CONTROL_ENVIRONMENT),
        *(name.upper() for name in explicit_names),
    }
    sanitized = {
        str(name): str(value)
        for name, value in inherited.items()
        if str(name).upper() in allowed_names or str(name).upper().startswith("LC_")
    }
    removed = tuple(name for name in inherited if name not in sanitized)
    target.clear()
    target.update(sanitized)
    return removed


def sanitize_worker_process_environment(
    *,
    allowed_environment_names: tuple[str, ...] = (),
    environment: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Strictly retain only claim controls and declared workflow variables."""

    target = os.environ if environment is None else environment
    explicit_names = validate_worker_environment_allowlist(
        list(allowed_environment_names)
    )
    inherited = dict(target)
    allowed_names = {
        *_SAFE_HELPER_ENVIRONMENT,
        *(name.upper() for name in SUPERVISOR_CONTROL_ENVIRONMENT),
        *(name.upper() for name in RUNNER_CONTROL_ENVIRONMENT),
        *(name.upper() for name in explicit_names),
    }
    sanitized = {
        str(name): str(value)
        for name, value in inherited.items()
        if str(name).upper() in allowed_names or str(name).upper().startswith("LC_")
    }
    removed = tuple(name for name in inherited if name not in sanitized)
    target.clear()
    target.update(sanitized)
    return removed


def discard_runner_token_from_environment(
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Remove the bearer token after the API client has captured it in memory."""

    target = os.environ if environment is None else environment
    target.pop("SIMSTACK_RUNNER_TOKEN", None)


def worker_environment_allowlist_value(names: tuple[str, ...]) -> str:
    """Serialize an already validated worker allowlist for a child process."""

    validated = validate_worker_environment_allowlist(list(names))
    return json.dumps(validated, separators=(",", ":"))


def worker_environment_allowlist_from_environment(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Read the exact worker allowlist passed by a trusted launcher."""

    source = os.environ if environment is None else environment
    raw = source.get("SIMSTACK_WORKER_ENVIRONMENT_ALLOWLIST", "[]")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "SIMSTACK_WORKER_ENVIRONMENT_ALLOWLIST must be a JSON list"
        ) from exc
    return validate_worker_environment_allowlist(value)


def assert_database_runtime_unavailable() -> None:
    """Refuse a runner environment that can import a database client/ODM."""

    available = [
        package
        for package in _DATABASE_RUNTIME_PACKAGES
        if importlib.util.find_spec(package) is not None
    ]
    if available:
        raise RuntimeError(
            "API-only runner runtime contains forbidden database packages: "
            + ", ".join(available)
        )


def validate_worker_environment_allowlist(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(name, str) and name for name in value
    ):
        raise RuntimeError("worker_environment_allowlist must be an explicit list")
    if len(value) != len(set(value)):
        raise RuntimeError("worker_environment_allowlist contains duplicates")
    control_names = {name.upper() for name in RUNNER_CONTROL_ENVIRONMENT}
    for name in value:
        normalized = name.upper()
        if not _ENVIRONMENT_NAME.fullmatch(name):
            raise RuntimeError(f"Invalid worker environment variable name: {name!r}")
        if normalized in control_names or normalized.startswith("SIMSTACK_"):
            raise RuntimeError(f"Runner control variable cannot be allowlisted: {name}")
        if is_blocked_runner_environment(name):
            raise RuntimeError(
                f"Database/config variable cannot be allowlisted: {name}"
            )
    return tuple(value)


def sanitized_trusted_helper_environment(
    *, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build a token-free environment for trusted git/scheduler helpers."""

    inherited = os.environ if source is None else source
    return {
        str(name): str(value)
        for name, value in inherited.items()
        if str(name).upper() in _SAFE_HELPER_ENVIRONMENT
        or str(name).upper().startswith("LC_")
    }


def sanitized_launcher_environment(
    control_environment: Mapping[str, str],
    *,
    allowed_environment_names: tuple[str, ...] = (),
    require_allowed: bool = True,
    source: Mapping[str, str] | None = None,
    workflow_bootstrap: bool = False,
) -> dict[str, str]:
    """Build a helper-capable launcher env plus explicit claim controls."""

    launcher = sanitized_trusted_helper_environment(source=source)
    worker = sanitized_runner_environment(
        control_environment,
        allowed_environment_names=allowed_environment_names,
        require_allowed=require_allowed,
        source=source,
        workflow_bootstrap=workflow_bootstrap,
    )
    launcher.update(worker)
    return launcher


def sanitized_workflow_subprocess_environment(
    allowed_environment_names: tuple[str, ...],
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Strip API controls while retaining only explicit workflow variables."""

    return sanitized_runner_environment(
        {},
        allowed_environment_names=allowed_environment_names,
        source=source,
        include_runtime_controls=False,
    )


def slurm_export_argument(
    environment: Mapping[str, str], *, extra_names: tuple[str, ...] = ()
) -> str:
    """Export only explicit control values and harmless process essentials."""

    safe_process_names = (
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    )
    names = [name for name in safe_process_names if name in environment]
    names.extend(name for name in RUNNER_CONTROL_ENVIRONMENT if name in environment)
    names.extend(name for name in RUNNER_RUNTIME_ENVIRONMENT if name in environment)
    names.extend(name for name in extra_names if name in environment)
    # --cleanenv containers read explicit APPTAINERENV_* values, not the
    # unprefixed batch environment. Export only counterparts of already
    # approved names; arbitrary inherited container variables remain excluded.
    names = list(dict.fromkeys(names))
    names.extend(
        f"APPTAINERENV_{name}"
        for name in tuple(names)
        if f"APPTAINERENV_{name}" in environment
    )
    if not all(name in environment for name in RUNNER_CONTROL_ENVIRONMENT):
        missing = [
            name for name in RUNNER_CONTROL_ENVIRONMENT if name not in environment
        ]
        raise ValueError("Missing Slurm runner environment: " + ", ".join(missing))
    return "--export=" + ",".join(names)


__all__ = [
    "RUNNER_CONTROL_ENVIRONMENT",
    "RUNNER_RUNTIME_ENVIRONMENT",
    "SUPERVISOR_CONTROL_ENVIRONMENT",
    "assert_database_runtime_unavailable",
    "discard_runner_token_from_environment",
    "is_blocked_runner_environment",
    "sanitize_runner_process_environment",
    "sanitize_worker_process_environment",
    "sanitized_trusted_helper_environment",
    "sanitized_launcher_environment",
    "sanitized_runner_environment",
    "sanitized_workflow_subprocess_environment",
    "slurm_export_argument",
    "validate_worker_environment_allowlist",
    "worker_environment_allowlist_from_environment",
    "worker_environment_allowlist_value",
]
