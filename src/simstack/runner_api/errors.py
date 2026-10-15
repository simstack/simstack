"""Public errors for runner API configuration and transport failures."""


class RunnerApiError(RuntimeError):
    """The runner control-plane API rejected or could not serve a request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RunnerApiConfigurationError(RunnerApiError):
    """Mandatory API-only runner configuration is absent or invalid."""
