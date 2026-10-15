"""Execution-layer failures for API-owned runner tasks."""


class RunnerTaskExecutionError(RuntimeError):
    """An API-owned task cannot be executed safely."""


class RunnerTaskPayloadError(RunnerTaskExecutionError):
    """A task bundle is incomplete or malformed."""
