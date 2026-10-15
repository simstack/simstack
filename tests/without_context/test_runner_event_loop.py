from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("embedding", [False, True])
def test_fresh_runner_loop_supports_sync_wait_without_consuming_outer_callback_frame(embedding):
    script = """
import asyncio, sys
from simstack.runner_runtime.event_loop import configure_worker_event_loop, prepare_embedded_runner_loop
embedding = sys.argv[1] == 'True'
async def execute():
    if embedding:
        await prepare_embedded_runner_loop()
    loop = asyncio.get_running_loop()
    assert getattr(loop, '_nest_patched', False)
    # An already-active outer _run_once frame used to crash when a first sync
    # child patched and consumed the remaining ready callbacks midway through.
    for _ in range(3):
        loop.call_soon(lambda: None)
    loop.run_until_complete(asyncio.sleep(0))
    await asyncio.sleep(0)
if not embedding:
    configure_worker_event_loop()
asyncio.run(execute())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(embedding)],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
