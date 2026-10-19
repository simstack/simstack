"""Root task lifecycle, lease renewal, and terminal API mutations."""

from __future__ import annotations

import asyncio
import logging
import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simstack.core.context import context
from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.node_runner import NodeRunner
from simstack.core.node import compute_arg_hash
from simstack.core.node_code_version import node_code_version
from simstack.core.task_id import clear_task_id, set_task_id
from simstack.models.document import ObjectId
from simstack.runner_runtime.logging import flush_runner_api_logs
from simstack.runner_runtime.config import runner_task_workdir
from simstack.runner_runtime.event_loop import prepare_embedded_runner_loop

from .config import lease_seconds
from .errors import RunnerTaskExecutionError, RunnerTaskPayloadError
from .hydration import _hydrate_entries, node_registry_from_task
from .mappings import _function_for_registry, _node_model_for_registry
from .nested import _root_call_arguments, _validate_input_contract
from .serialization import cached_runner_commit, serialize_runner_result
from .state import (
    RunnerClaimContext,
    activate_runner_claim,
    reset_runner_claim,
)

logger = logging.getLogger("RunnerTaskExecution")


async def _renew_lease_until_stopped(
    claim: RunnerClaimContext,
    *,
    lease_seconds: int,
    stop: asyncio.Event,
) -> None:
    renew_every = max(10.0, lease_seconds / 3)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=renew_every)
            return
        except asyncio.TimeoutError:
            await context.runner_api.renew_lease(
                claim.task_id,
                claim_id=claim.claim_id,
                attempt=claim.attempt,
                lease_seconds=lease_seconds,
            )


async def _run_with_lease(
    operation: Callable[[], Awaitable[Any]],
    claim: RunnerClaimContext,
    terminal_request: asyncio.Event,
) -> Any:
    stop = asyncio.Event()
    execution_task = asyncio.create_task(operation())
    lease_task = asyncio.create_task(
        _renew_lease_until_stopped(
            claim,
            lease_seconds=lease_seconds(),
            stop=stop,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {execution_task, lease_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if lease_task in done:
            try:
                await lease_task
            except Exception:
                if not terminal_request.is_set():
                    raise
                # A successful terminal mutation immediately revokes active
                # lease renewal. Let its bounded HTTP response establish the
                # outcome rather than turning that race into a false failure.
                # The Server still fences the mutation by claim and lease.
                return await execution_task
            if not execution_task.done():  # pragma: no cover - only stop/error exits it
                raise RunnerTaskExecutionError(
                    "Task lease renewal stopped unexpectedly"
                )
        return await execution_task
    finally:
        stop.set()
        if not execution_task.done():
            execution_task.cancel()
        await asyncio.gather(execution_task, lease_task, return_exceptions=True)


async def execute_claimed_task(
    task: Mapping[str, Any],
    *,
    claim_id: str,
    attempt: int,
) -> bool:
    """Execute and commit one API-owned root task without any DB access."""

    await prepare_embedded_runner_loop()
    registry = node_registry_from_task(task)
    task_id = str(registry.id)
    claim = RunnerClaimContext(
        task_id=task_id,
        claim_id=claim_id,
        attempt=attempt,
        call_path=registry.call_path,
    )
    original_dir = Path.cwd()
    execution_token = None
    try:
        node_model = _node_model_for_registry(registry)
        function = _function_for_registry(registry, node_model=node_model)
        input_entries = _hydrate_entries(task.get("inputs"), kind="inputs")
        _validate_input_contract(
            input_entries,
            node_model.input_mappings,
            kind="Root node",
        )
        input_args, input_kwargs = _root_call_arguments(function, input_entries)
        actual_arg_hash = compute_arg_hash([value for _, value in input_entries])
        if registry.arg_hash not in ("", "NOT INITIALIZED", actual_arg_hash):
            raise RunnerTaskPayloadError(
                "Task argument hash does not match hydrated inputs"
            )
        registry.function_hash = node_code_version(
            function, workflow_revision=registry.workflow_revision
        )
        registry.arg_hash = actual_arg_hash
        registry.version = getattr(function, "_node_version", None)
        registry.is_async = inspect.iscoroutinefunction(function)
        expected_status = (
            registry.status.value
            if isinstance(registry.status, TaskStatus)
            else str(registry.status)
        )
        await context.runner_api.update_task_status(
            task_id,
            claim_id=claim_id,
            attempt=attempt,
            expected_status=expected_status,
            status=TaskStatus.RUNNING.value,
            observed_at=datetime.now(UTC).isoformat(),
            function_hash=registry.function_hash,
            arg_hash=registry.arg_hash,
            version=registry.version,
            is_async=registry.is_async,
        )

        node_runner = NodeRunner(registry.name, task_id)
        node_kwargs: dict[str, Any] = {
            "node_runner": node_runner,
            "parent_id": registry.id,
            "task_id": registry.id,
            "call_path": registry.call_path,
            "parent_parameters": registry.parameters,
            "recompute_artifacts": registry.parameters.recompute_artifacts,
            "custom_name": registry.custom_name,
            "arg_hash": registry.arg_hash,
            "function_hash": registry.function_hash,
        }
        if registry.parameters.force_rerun:
            node_kwargs["force_rerun"] = True
        signature = inspect.signature(function)
        try:
            bound_inputs = signature.bind_partial(*input_args, **input_kwargs)
        except TypeError as exc:
            raise RunnerTaskPayloadError(
                "Runner task inputs cannot bind to the node signature"
            ) from exc
        accepts_metadata_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        node_kwargs = {
            name: value
            for name, value in node_kwargs.items()
            if name not in bound_inputs.arguments
            and name not in input_kwargs
            and (
                accepts_metadata_kwargs
                or (
                    name in signature.parameters
                    and signature.parameters[name].kind
                    in {
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    }
                )
            )
        }
        node_kwargs.update(input_kwargs)

        workdir = runner_task_workdir(Path(context.config.workdir), task_id)
        workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        workdir.chmod(0o700)
        os.chdir(workdir)
        set_task_id(ObjectId(task_id))
        execution_token = activate_runner_claim(claim)
        terminal_request = asyncio.Event()

        async def execute_and_commit():
            cached = await context.runner_api.resolve_task_cache(
                task_id,
                claim_id=claim_id,
                attempt=attempt,
            )
            if cached is not None:
                commit = cached_runner_commit(
                    cached, result_mappings=node_model.result_mappings
                )
            else:
                if asyncio.iscoroutinefunction(function):
                    result = await function(*input_args, **node_kwargs)
                else:
                    result = await asyncio.to_thread(
                        function, *input_args, **node_kwargs
                    )
                from .blobs import publish_runtime_blobs

                await asyncio.to_thread(node_runner._make_log_file)
                await asyncio.to_thread(publish_runtime_blobs, [result, *node_runner.info_files])
                commit = serialize_runner_result(
                    result,
                    runner_info_files=node_runner.info_files,
                    result_mappings=node_model.result_mappings,
                )
            logger.info("Committing runner task %s (attempt %s)", task_id, attempt)
            await asyncio.to_thread(flush_runner_api_logs)
            terminal_request.set()
            await context.runner_api.commit_task(
                task_id,
                claim_id=claim_id,
                attempt=attempt,
                expected_status=TaskStatus.RUNNING.value,
                results=commit.results,
                info_files=commit.info_files,
                message=commit.message,
                return_kind=commit.return_kind,
                custom_name=commit.custom_name,
                expected_custom_name=registry.custom_name,
            )

        await _run_with_lease(execute_and_commit, claim, terminal_request)
        return True
    except Exception as exc:
        logger.exception("API-only execution failed for task %s", task_id)
        try:
            await asyncio.to_thread(flush_runner_api_logs)
            await context.runner_api.fail_task(
                task_id,
                claim_id=claim_id,
                attempt=attempt,
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            logger.exception("Could not report failure for runner task %s", task_id)
        return False
    finally:
        if execution_token is not None:
            reset_runner_claim(execution_token)
        clear_task_id()
        os.chdir(original_dir)
