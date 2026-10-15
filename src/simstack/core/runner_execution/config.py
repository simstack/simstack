"""Validated runtime controls for claim execution."""

import os
import math

from simstack.runner_protocol import (
    DEFAULT_RUNNER_LEASE_SECONDS,
    MAX_RUNNER_LEASE_SECONDS,
    MIN_RUNNER_LEASE_SECONDS,
)

from .errors import RunnerTaskExecutionError

CHILD_POLL_SECONDS_ENV = "SIMSTACK_RUNNER_CHILD_POLL_SECONDS"
LEASE_SECONDS_ENV = "SIMSTACK_RUNNER_LEASE_SECONDS"
DEFAULT_CHILD_POLL_SECONDS = 5.0


def child_poll_seconds() -> float:
    raw_value = os.environ.get(CHILD_POLL_SECONDS_ENV, str(DEFAULT_CHILD_POLL_SECONDS))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RunnerTaskExecutionError(
            f"{CHILD_POLL_SECONDS_ENV} must be a number"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise RunnerTaskExecutionError(
            f"{CHILD_POLL_SECONDS_ENV} must be finite and positive"
        )
    return value


def lease_seconds() -> int:
    raw_value = os.environ.get(LEASE_SECONDS_ENV, str(DEFAULT_RUNNER_LEASE_SECONDS))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RunnerTaskExecutionError(
            f"{LEASE_SECONDS_ENV} must be an integer"
        ) from exc
    if not MIN_RUNNER_LEASE_SECONDS <= value <= MAX_RUNNER_LEASE_SECONDS:
        raise RunnerTaskExecutionError(
            f"{LEASE_SECONDS_ENV} must be between {MIN_RUNNER_LEASE_SECONDS} "
            f"and {MAX_RUNNER_LEASE_SECONDS}"
        )
    return value
