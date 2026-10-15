from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from simstack.runner_runtime.logging import RunnerApiLogHandler


@pytest.mark.parametrize(
    ("bound_task_id", "message", "expected_task_id"),
    [
        (None, "supervisor failure task_id: spoofed-task", None),
        ("bound-task", "worker failure task_id: spoofed-task", "bound-task"),
    ],
)
def test_runner_api_log_task_provenance_is_constructor_bound(
    monkeypatch,
    bound_task_id,
    message,
    expected_task_id,
):
    monkeypatch.setattr(RunnerApiLogHandler, "_run", lambda self: None)
    handler = RunnerApiLogHandler(
        SimpleNamespace(timeout_seconds=0.1),
        task_id=bound_task_id,
    )
    try:
        handler.emit(
            logging.LogRecord(
                name="runner-test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg=message,
                args=(),
                exc_info=None,
            )
        )
        entry = handler._queue.get_nowait()
        handler._queue.task_done()
    finally:
        handler.close()

    assert entry["task_id"] == expected_task_id
