"""Errors raised while validating the versioned runner wire protocol."""


class RunnerProtocolError(RuntimeError):
    """A payload or path does not match the exact runner protocol contract."""
