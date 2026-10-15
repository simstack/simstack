from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters, Resource


@pytest.mark.asyncio
async def test_default_queue_with_explicit_docker_flag_uses_container_api_dispatch(monkeypatch):
    api = object()
    registry = NodeRegistry(name="calculation", function_hash="code", arg_hash="arguments",
                            func_mapping="package.calculation", parameters=Parameters(in_docker=True),
                            status=TaskStatus.RETRIEVED)
    docker = AsyncMock(return_value=True)
    monkeypatch.setattr("simstack.core.services.node_execution_service.run_docker", docker)
    monkeypatch.setattr("simstack.core.services.node_execution_service.context",
                        SimpleNamespace(config=SimpleNamespace(resource="self")))
    service = NodeExecutionService(Resource(value="self"), interval=1, max_concurrent=1,
                                   shutdown_event=None)
    monkeypatch.setattr(service, "write_node_event", AsyncMock())
    assert await service.run_node(registry, claim_id="claimed", attempt=1, execution_api=api)
    assert docker.await_args.kwargs["runner_api"] is api
    assert docker.await_args.kwargs["claim_id"] == "claimed"


@pytest.mark.asyncio
async def test_slurm_queue_with_docker_flag_first_enters_allocation(monkeypatch):
    api = object()
    registry = NodeRegistry(name="calculation", function_hash="code", arg_hash="arguments",
                            func_mapping="package.calculation",
                            parameters=Parameters(queue="slurm-queue", in_docker=True),
                            status=TaskStatus.RETRIEVED)
    submit = AsyncMock(return_value=True)
    docker = AsyncMock(return_value=True)
    monkeypatch.setattr("simstack.core.services.node_execution_service.submit_node", submit)
    monkeypatch.setattr("simstack.core.services.node_execution_service.run_docker", docker)
    monkeypatch.setattr("simstack.core.services.node_execution_service.context",
                        SimpleNamespace(config=SimpleNamespace(resource="self")))
    service = NodeExecutionService(Resource(value="self"), interval=1, max_concurrent=1,
                                   shutdown_event=None)
    monkeypatch.setattr(service, "write_node_event", AsyncMock())
    assert await service.run_node(registry, claim_id="claimed", attempt=1, execution_api=api)
    submit.assert_awaited_once()
    docker.assert_not_awaited()
