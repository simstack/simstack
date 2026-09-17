import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.resources import allowed_resources
from simstack.core.services.slurm_status_service import (
    SlurmStatusService,
    resource_uses_slurm_queue,
)
from simstack.models import NodeRegistry
from simstack.models.parameters import Parameters, Resource
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.runner_utils import SqueueNotFoundError, clean_slurm_info


async def _upsert_resource_definition(resource_str: str, queue: str) -> Resource:
    allowed_resources.add_resource(resource_str)
    existing = await context.db.find_one(
        ResourceDefinition, ResourceDefinition.resource_str == resource_str
    )
    if existing is None:
        await context.db.save(
            ResourceDefinition(
                resource_str=resource_str,
                workdir=f"/tmp/{resource_str}",
                hostname="localhost",
                queue=queue,
            )
        )
    else:
        existing.queue = queue
        await context.db.save(existing)
    return Resource(value=resource_str)


def _running_task(resource: str, job_id: str = "12345") -> NodeRegistry:
    return NodeRegistry(
        name="test_node",
        status=TaskStatus.RUNNING,
        function_hash="function-hash",
        arg_hash="arg-hash",
        func_mapping="test_mapping",
        parameters=Parameters(resource=resource),
        job_id=job_id,
    )


@pytest.mark.asyncio
async def test_resource_uses_slurm_queue_requires_slurm_definition(
    initialized_context,
):
    slurm_resource = await _upsert_resource_definition("slurm-host", "slurm-queue")
    default_resource = await _upsert_resource_definition("big1", "default")

    assert await resource_uses_slurm_queue(slurm_resource) is True
    assert await resource_uses_slurm_queue(default_resource) is False
    assert await resource_uses_slurm_queue(Resource(value="self")) is False


@pytest.mark.asyncio
async def test_slurm_status_skips_resources_without_slurm_queue(
    initialized_context, monkeypatch
):
    resource = await _upsert_resource_definition("big1", "default")
    await context.db.save(_running_task("big1"))
    clean = AsyncMock()
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.clean_slurm_info", clean
    )

    await SlurmStatusService(resource, interval=60).execute()

    clean.assert_not_called()


@pytest.mark.asyncio
async def test_clean_slurm_info_is_silent_when_squeue_missing(
    initialized_context, monkeypatch, caplog
):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(
        "simstack.util.runner_utils.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=127,
            stdout="",
            stderr="/bin/sh: 1: squeue: not found",
        ),
    )

    with pytest.raises(SqueueNotFoundError):
        await clean_slurm_info(Resource(value="self"))

    assert "Skipping slurm info cleanup" not in caplog.text
    assert "Error cleaning slurm info" not in caplog.text


@pytest.mark.asyncio
async def test_slurm_status_is_silent_when_squeue_missing(
    initialized_context, monkeypatch, caplog
):
    resource = await _upsert_resource_definition("slurm-host", "slurm-queue")
    task = await context.db.save(_running_task("slurm-host"))
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.clean_slurm_info",
        AsyncMock(side_effect=SqueueNotFoundError("squeue is not available")),
    )

    await SlurmStatusService(resource, interval=60).execute()

    saved = await context.db.find_one(NodeRegistry, NodeRegistry.id == task.id)
    assert saved.status == TaskStatus.RUNNING
    assert saved.job_id == "12345"
    assert "Error checking Slurm status" not in caplog.text
    assert "squeue" not in caplog.text.lower()
