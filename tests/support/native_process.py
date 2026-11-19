"""Replace both native process launchers while exercising the surrounding lifecycle."""
import asyncio
from unittest.mock import AsyncMock

from simstack.runner_runtime import windows_science_job


def mock_native_spawn(monkeypatch, spawn):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))
    monkeypatch.setattr(windows_science_job, "spawn_job_process",
                        lambda command, **kwargs: spawn(*command, **kwargs))
