from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters, Resource
from simstack.models.workflow_repository import CodeSource


@pytest.mark.asyncio
async def test_pinned_task_uses_trusted_detached_launcher(
    initialized_context,
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("SIMSTACK_SERVER_URL", raising=False)
    monkeypatch.delenv("SIMSTACK_RUNNER_TOKEN", raising=False)
    monkeypatch.setattr(context.config, "_server_url", "https://runner.example.invalid")
    monkeypatch.setattr(context.config, "_server_token", "runner-token")
    checkout = tmp_path / "uploaded-repository"
    checkout.mkdir()
    registry_entry = await context.db.save(
        NodeRegistry(
            name="pinned_node",
            status=TaskStatus.RETRIEVED,
            parameters=Parameters(),
            func_mapping="workflow.pinned_node",
            function_hash="a" * 40,
            arg_hash="arguments",
            code_source=CodeSource(repo_id=ObjectId(), commit="a" * 40),
        )
    )
    service = NodeExecutionService(
        Resource(value="test"),
        interval=1,
        max_concurrent=1,
        shutdown_event=asyncio.Event(),
        detach=False,
    )
    process = MagicMock(pid=1234)

    with (
        patch.object(service, "write_node_event", AsyncMock()),
        patch(
            "simstack.core.services.node_execution_service.materialize_task_checkout",
            AsyncMock(return_value=checkout),
        ),
        patch(
            "simstack.core.services.node_execution_service.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ) as spawn,
        patch(
            "simstack.core.services.node_execution_service.run_node_from_registry",
            AsyncMock(),
        ) as inline_run,
    ):
        assert await service.run_node(registry_entry) is True

    command = spawn.await_args.args
    options = spawn.await_args.kwargs
    assert command[:3] == (sys.executable, "-m", "simstack.core.run_node")
    assert command[-2:] == ("--project-root", str(checkout))
    assert options["cwd"] == context.config.project_root
    assert json.loads(options["env"]["SIMSTACK_TASK_PYTHON_PATHS"]) == [str(checkout)]
    assert options["env"]["SIMSTACK_SERVER_URL"] == "https://runner.example.invalid"
    assert options["env"]["SIMSTACK_RUNNER_TOKEN"] == "runner-token"
    inline_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_slurm_task_is_materialized_before_submission(
    initialized_context,
    tmp_path,
):
    registry_entry = await context.db.save(
        NodeRegistry(
            name="pinned_slurm_node",
            status=TaskStatus.RETRIEVED,
            parameters=Parameters(queue="slurm-queue"),
            func_mapping="workflow.pinned_slurm_node",
            function_hash="b" * 40,
            arg_hash="arguments",
            code_source=CodeSource(repo_id=ObjectId(), commit="b" * 40),
        )
    )
    service = NodeExecutionService(
        Resource(value="test"),
        interval=1,
        max_concurrent=1,
        shutdown_event=asyncio.Event(),
        detach=False,
    )
    calls = []

    async def materialize(*_args):
        calls.append("materialize")
        return tmp_path / "checkout"

    async def submit(*_args, **kwargs):
        calls.append(("submit", kwargs.get("repository_checkout")))
        return True

    with (
        patch.object(service, "write_node_event", AsyncMock()),
        patch(
            "simstack.core.services.node_execution_service.materialize_task_checkout",
            side_effect=materialize,
        ),
        patch(
            "simstack.core.services.node_execution_service.submit_node",
            side_effect=submit,
        ),
    ):
        assert await service.run_node(registry_entry) is True

    assert calls == ["materialize", ("submit", tmp_path / "checkout")]


@pytest.mark.asyncio
async def test_repository_task_rejects_legacy_custom_docker_queue(
    initialized_context,
    caplog,
):
    registry_entry = await context.db.save(
        NodeRegistry(
            name="pinned_docker_node",
            status=TaskStatus.RETRIEVED,
            parameters=Parameters(queue="docker"),
            func_mapping="workflow.pinned_docker_node",
            function_hash="c" * 40,
            arg_hash="arguments",
            code_source=CodeSource(repo_id=ObjectId(), commit="c" * 40),
        )
    )
    service = NodeExecutionService(
        Resource(value="test"),
        interval=1,
        max_concurrent=1,
        shutdown_event=asyncio.Event(),
        detach=False,
    )

    with (
        patch.object(service, "write_node_event", AsyncMock()),
        patch(
            "simstack.core.services.node_execution_service.materialize_task_checkout",
            AsyncMock(),
        ) as materialize,
        patch(
            "simstack.core.services.node_execution_service.run_docker",
            AsyncMock(),
        ) as docker_run,
    ):
        assert await service.run_node(registry_entry) is False

    assert "custom runner images are not supported" in caplog.text
    materialize.assert_not_awaited()
    docker_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_custom_docker_queue_remains_available(initialized_context):
    registry_entry = await context.db.save(
        NodeRegistry(
            name="legacy_docker_node",
            status=TaskStatus.RETRIEVED,
            parameters=Parameters(queue="docker"),
            func_mapping="workflow.legacy_docker_node",
            function_hash="d" * 40,
            arg_hash="arguments",
        )
    )
    service = NodeExecutionService(
        Resource(value="test"),
        interval=1,
        max_concurrent=1,
        shutdown_event=asyncio.Event(),
        detach=False,
    )

    with (
        patch.object(service, "write_node_event", AsyncMock()),
        patch(
            "simstack.core.services.node_execution_service.materialize_task_checkout",
            AsyncMock(),
        ) as materialize,
        patch(
            "simstack.core.services.node_execution_service.run_docker",
            AsyncMock(return_value=True),
        ) as docker_run,
    ):
        assert await service.run_node(registry_entry) is True

    materialize.assert_not_awaited()
    docker_run.assert_awaited_once_with(registry_entry)
