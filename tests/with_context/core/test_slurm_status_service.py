from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.services.slurm_status_service import SlurmStatusService
from simstack.models import NodeRegistry
from simstack.models.parameters import Parameters, Resource
from simstack.models.slurm_info import SlurmInfo
from simstack.util.runner_utils import SqueueQueryError


def _running_task(job_id: str = "12345") -> NodeRegistry:
    return NodeRegistry(
        name="hyperpolarizibility",
        status=TaskStatus.RUNNING,
        function_hash="function-hash",
        arg_hash="arg-hash",
        func_mapping="hyperpolarizibility.workflows.hyperpolarizibility",
        parameters=Parameters(resource="self"),
        job_id=job_id,
    )


@pytest.mark.asyncio
async def test_squeue_failure_does_not_mark_running_job_timeout(
    initialized_context, monkeypatch
):
    resource = Resource(value="self")
    task = await context.db.save(_running_task())
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.clean_slurm_info",
        AsyncMock(),
    )

    def fail_squeue(job_id, task_id, query_resource):
        raise SqueueQueryError(f"squeue for job {job_id} timed out after 30s")

    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.get_job_info", fail_squeue
    )

    await SlurmStatusService(resource, interval=60).execute()

    saved = await context.db.find_one(NodeRegistry, NodeRegistry.id == task.id)
    assert saved.status == TaskStatus.RUNNING
    assert saved.job_id == "12345"


@pytest.mark.asyncio
async def test_successful_empty_queue_marks_running_job_timeout(
    initialized_context, monkeypatch
):
    resource = Resource(value="self")
    task = await context.db.save(_running_task())
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.clean_slurm_info",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.get_job_info",
        lambda *args, **kwargs: None,
    )

    await SlurmStatusService(resource, interval=60).execute()

    saved = await context.db.find_one(NodeRegistry, NodeRegistry.id == task.id)
    assert saved.status == TaskStatus.TIME_OUT
    assert saved.job_id is None


@pytest.mark.asyncio
async def test_successful_squeue_keeps_running_job_and_stores_info(
    initialized_context, monkeypatch
):
    resource = Resource(value="self")
    task = await context.db.save(_running_task())
    slurm_info = SlurmInfo(
        node_registry=task.id,
        resource=resource,
        job_id="12345",
        updated=datetime.now(),
        name="hyperpol",
        user="user",
        code="R",
        time="1:23",
        nodes=["node01"],
    )
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.clean_slurm_info",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "simstack.core.services.slurm_status_service.get_job_info",
        lambda *args, **kwargs: slurm_info,
    )

    await SlurmStatusService(resource, interval=60).execute()

    saved = await context.db.find_one(NodeRegistry, NodeRegistry.id == task.id)
    stored = await context.db.find_one(SlurmInfo, SlurmInfo.job_id == "12345")
    assert saved.status == TaskStatus.RUNNING
    assert saved.job_id == "12345"
    assert stored is not None
    assert stored.code == "R"
    assert stored.time == "1:23"
