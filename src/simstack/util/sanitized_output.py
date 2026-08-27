from collections.abc import Iterable
import re


REDACTED = "***REDACTED***"
DEFAULT_OUTPUT_LIMIT = 4096
_URI_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<credentials>[^/@\s]+)@"
)


def redact_connection_string(text: str, connection_string: str | None) -> str:
    """Remove the configured database URI from text that may reach logs or tasks."""
    if connection_string:
        text = text.replace(connection_string, REDACTED)
    return _URI_CREDENTIALS.sub(r"\g<scheme>***REDACTED***@", text)


def bounded_tail(text: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    """Keep the useful end of child output without persisting an unbounded log."""
    if len(text) <= limit:
        return text
    prefix = "[truncated]\n"
    tail_size = max(limit - len(prefix), 0)
    tail = text[-tail_size:] if tail_size else ""
    return f"{prefix[:limit]}{tail}"


def sanitized_tail(
    value: bytes | str | None,
    connection_string: str | None,
    *,
    limit: int = DEFAULT_OUTPUT_LIMIT,
) -> str:
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = value or ""
    return bounded_tail(redact_connection_string(text, connection_string), limit).strip()


def sanitized_command(
    command: Iterable[object], connection_string: str | None
) -> list[str]:
    return [redact_connection_string(str(argument), connection_string) for argument in command]
