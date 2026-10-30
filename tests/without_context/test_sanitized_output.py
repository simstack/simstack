from simstack.util.sanitized_output import (
    DEFAULT_OUTPUT_LIMIT,
    REDACTED,
    sanitized_tail,
)


def test_sanitized_tail_keeps_full_text_when_limit_is_none():
    secret = "mongodb://user:password@db.internal/simstack"
    text = "head-marker " + ("x" * (DEFAULT_OUTPUT_LIMIT * 2)) + f" {secret} tail-marker"

    cleaned = sanitized_tail(text, secret, limit=None)

    assert "[truncated]" not in cleaned
    assert "head-marker" in cleaned
    assert "tail-marker" in cleaned
    assert secret not in cleaned
    assert REDACTED in cleaned
    assert len(cleaned) > DEFAULT_OUTPUT_LIMIT


def test_sanitized_tail_still_bounds_subprocess_output_by_default():
    text = "head-marker " + ("x" * (DEFAULT_OUTPUT_LIMIT * 2)) + " tail-marker"

    cleaned = sanitized_tail(text, None)

    assert cleaned.startswith("[truncated]")
    assert "head-marker" not in cleaned
    assert "tail-marker" in cleaned
    assert len(cleaned) <= DEFAULT_OUTPUT_LIMIT
