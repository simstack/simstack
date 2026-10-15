"""Bounded API-backed logging for runner processes."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from simstack.runner_protocol.logs import log_record_payload
from simstack.runner_api import BaseRunnerClient


class RunnerApiLogHandler(logging.Handler):
    """Bounded, non-blocking LogRecord delivery to the runner API."""

    def __init__(
        self,
        client: BaseRunnerClient,
        *,
        capacity: int = 2_000,
        batch_size: int = 100,
        flush_interval_seconds: float = 0.25,
        task_id: str | None = None,
    ) -> None:
        super().__init__()
        if capacity < 1 or batch_size < 1 or flush_interval_seconds <= 0:
            raise ValueError("Runner API log queue settings must be positive")
        self._client = client
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=capacity)
        self._batch_size = batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._task_id = str(task_id) if task_id is not None else None
        self._stop = threading.Event()
        self.dropped_records = 0
        self.delivery_failures = 0
        self._worker = threading.Thread(
            target=self._run,
            name="simstack-runner-api-logs",
            daemon=True,
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = log_record_payload(record, formatter=self.formatter)
            # The token boundary, not message text or ambient ContextVars,
            # owns log provenance. Supervisors always emit resource logs with
            # ``task_id=None``; execution workers are bound to the exact task
            # passed at handler construction.
            entry["task_id"] = self._task_id
            self._queue.put_nowait(entry)
        except queue.Full:
            self.dropped_records += 1
        except Exception:
            self.handleError(record)

    def _take_batch(self, *, block: bool) -> list[dict[str, Any]]:
        try:
            first = self._queue.get(
                block=block,
                timeout=self._flush_interval_seconds if block else None,
            )
        except queue.Empty:
            return []
        batch = [first]
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _deliver(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            self._client.send_logs_sync(logs=batch)
        except Exception:
            # Delivery is intentionally not retried forever: logging must never
            # deadlock execution or grow without bound.
            self.delivery_failures += len(batch)
        finally:
            for _ in batch:
                self._queue.task_done()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            self._deliver(self._take_batch(block=not self._stop.is_set()))

    def flush(self) -> None:
        if threading.current_thread() is self._worker:
            return
        self._queue.join()

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=max(1.0, self._client.timeout_seconds + 1.0))
        super().close()


def flush_runner_api_logs() -> None:
    """Drain every installed runner API handler before a terminal mutation."""

    seen: set[int] = set()
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if isinstance(handler, RunnerApiLogHandler) and id(handler) not in seen:
                seen.add(id(handler))
                handler.flush()


__all__ = ["RunnerApiLogHandler", "flush_runner_api_logs"]
