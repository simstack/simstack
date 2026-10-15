"""Prepare reentrant API waits before invoking untrusted workflow code."""

from __future__ import annotations

import asyncio

import nest_asyncio


def configure_worker_event_loop() -> None:
    """Configure the standalone interpreter before its event loop runs."""
    nest_asyncio.apply()


async def prepare_embedded_runner_loop() -> None:
    """Safely configure an already-running embedding loop at a yield boundary.

    Patching from a synchronous nested call is too late: its nested execution
    may consume callbacks belonging to the original _run_once frame. Yield
    once before workflow invocation so that frame has finished first.
    """
    loop = asyncio.get_running_loop()
    if not getattr(loop, "_nest_patched", False):
        nest_asyncio.apply(loop)
        await asyncio.sleep(0)
