"""Claim-bound worker lifecycle and bounded, credential-safe diagnostics.

Local, container and inline workers share this process boundary. A worker exit
must not keep a supervisor slot occupied because descendants inherited pipes,
and descendants must not outlive the task that owns them.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_DIAGNOSTIC_STREAM_BYTES = 8 * 1024
_DIAGNOSTIC_STREAM_LINES = 80
_DIAGNOSTIC_DRAIN_TIMEOUT_SECONDS = 1.0
_REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class WorkerProcessResult:
    returncode: int
    stdout: str
    stderr: str


async def wait_for_worker_exit(process: asyncio.subprocess.Process) -> int:
    # Process.wait()/communicate() may wait for inherited PIPE descriptors even
    # after the direct child exited. The child watcher's returncode does not.
    while process.returncode is None:
        await asyncio.sleep(0.05)
    return process.returncode


def signal_worker_tree(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        if os.name != "nt":
            os.killpg(process.pid, sig)
        elif process.returncode is None:
            process.terminate() if sig == signal.SIGTERM else process.kill()
    except ProcessLookupError:
        pass


async def terminate_worker_tree(
    process: asyncio.subprocess.Process, *, timeout_seconds: float = 5.0
) -> None:
    signal_worker_tree(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(wait_for_worker_exit(process), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass
    # The direct child exiting does not prove descendants exited. Always finish
    # the process group, including descendants that deliberately ignore TERM.
    await asyncio.sleep(0.1)
    signal_worker_tree(process, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
    if process.returncode is None:
        await asyncio.wait_for(wait_for_worker_exit(process), timeout=timeout_seconds)


async def read_worker_stream(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    tail = bytearray()
    total_bytes = 0
    while chunk := await stream.read(4096):
        total_bytes += len(chunk)
        tail.extend(chunk)
        if len(tail) > _DIAGNOSTIC_STREAM_BYTES:
            del tail[: len(tail) - _DIAGNOSTIC_STREAM_BYTES]
    lines = tail.decode("utf-8", errors="replace").splitlines()
    line_truncated = len(lines) > _DIAGNOSTIC_STREAM_LINES
    text = "\n".join(lines[-_DIAGNOSTIC_STREAM_LINES:])
    if total_bytes > len(tail) or line_truncated:
        text = f"[earlier worker output truncated]\n{text}"
    return text


async def _finish_stream(task: asyncio.Task[str]) -> str:
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=_DIAGNOSTIC_DRAIN_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return "[worker output stream did not close]"


async def supervise_worker(
    process: asyncio.subprocess.Process,
    *,
    renew_lease: Callable[[], Awaitable[object]] | None = None,
    renew_every: float | None = None,
) -> WorkerProcessResult:
    """Wait independently of pipe EOF, renewing ownership while the child runs."""
    if renew_lease is not None and (renew_every is None or renew_every <= 0):
        raise ValueError("Worker lease renewal interval must be positive")
    stdout_task = asyncio.create_task(read_worker_stream(process.stdout))
    stderr_task = asyncio.create_task(read_worker_stream(process.stderr))
    wait_task = asyncio.create_task(wait_for_worker_exit(process))
    try:
        while True:
            try:
                returncode = await asyncio.wait_for(
                    asyncio.shield(wait_task), timeout=renew_every
                )
                break
            except asyncio.TimeoutError:
                assert renew_lease is not None
                await renew_lease()
    finally:
        try:
            await terminate_worker_tree(process)
        finally:
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)
            stdout, stderr = await asyncio.gather(
                _finish_stream(stdout_task), _finish_stream(stderr_task)
            )
    return WorkerProcessResult(returncode, stdout, stderr)


def worker_failure_diagnostic(
    stdout: str, stderr: str, *, secrets: tuple[str, ...]
) -> str:
    sections = []
    for name, value in (("stderr", stderr), ("stdout", stdout)):
        sanitized = sanitize_worker_diagnostic(value, secrets=secrets)
        if sanitized:
            sections.append(f"{name}:\n{sanitized}")
    return "\n".join(sections)


def sanitize_worker_diagnostic(value: str, *, secrets: tuple[str, ...]) -> str:
    sanitized = value
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, _REDACTED)
    sanitized = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+",
        rf"\1{_REDACTED}", sanitized,
    )
    sanitized = re.sub(
        r"(?i)(SIMSTACK_RUNNER_TOKEN\s*[:=]\s*)[^\s]+",
        rf"\1{_REDACTED}", sanitized,
    )
    return "".join(
        character for character in sanitized
        if character in "\n\t" or character.isprintable()
    ).strip()
