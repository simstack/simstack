from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from simstack.core.context import context
from simstack.core.runner_execution import execute_claimed_task
from simstack.runner_api import ExecutionRunnerClient, RunnerApiConfigurationError
from simstack.runner_runtime.event_loop import configure_worker_event_loop
from simstack.runner_runtime.environment import (
    discard_runner_token_from_environment,
    sanitize_worker_process_environment,
    worker_environment_allowlist_from_environment,
)


logger = logging.getLogger("RunNode")


def _required_value(value: str | None, *, name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{name} is required for API-only task execution")
    return value.strip()


def _required_attempt(value: int | str | None) -> int:
    if value is None or str(value).strip() == "":
        raise ValueError("SIMSTACK_TASK_ATTEMPT/--attempt is required")
    try:
        attempt = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Task attempt must be an integer") from exc
    if attempt < 1:
        raise ValueError("Task attempt must be positive")
    return attempt


async def run_node_from_id(
    node_id: str,
    resource_str: str | None,
    project_root: str | Path | None = None,
    *,
    claim_id: str,
    attempt: int,
) -> bool:
    """Run one server-owned task using only its claim-scoped execution API."""

    task_id = _required_value(node_id, name="SIMSTACK_TASK_ID/--node-id")
    active_claim_id = _required_value(
        claim_id, name="SIMSTACK_TASK_CLAIM_ID/--claim-id"
    )
    active_attempt = _required_attempt(attempt)
    environment_allowlist = worker_environment_allowlist_from_environment()
    sanitize_worker_process_environment(allowed_environment_names=environment_allowlist)
    logger.info(
        "Initializing API-only task %s (claim %s, attempt %s)",
        task_id,
        active_claim_id,
        active_attempt,
    )
    try:
        failure_client = ExecutionRunnerClient.from_environment()
    except RunnerApiConfigurationError:
        failure_client = None
    try:
        await context.initialize_runner(
            resource=resource_str,
            project_root=project_root,
            with_file_transfer=True,
            task_id=task_id,
            claim_id=active_claim_id,
            attempt=active_attempt,
        )
        discard_runner_token_from_environment()
        envelope = await context.runner_api.get_task(
            task_id,
            claim_id=active_claim_id,
            attempt=active_attempt,
        )
        if envelope.claim_id != active_claim_id or envelope.attempt != active_attempt:
            raise RuntimeError("Runner API returned a different task claim")
        returned_task_id = envelope.task.get("id")
        if str(returned_task_id or "") != task_id:
            raise RuntimeError("Runner API returned a different task ID")
        return await execute_claimed_task(
            envelope.task,
            claim_id=active_claim_id,
            attempt=active_attempt,
        )
    except Exception as exc:
        logger.exception("API-only task execution failed for %s", task_id)
        try:
            client = context.runner_api or failure_client
        except RuntimeError:
            client = failure_client
        if client is not None:
            try:
                await client.fail_task(
                    task_id, claim_id=active_claim_id, attempt=active_attempt,
                    error=type(exc).__name__, message=str(exc),
                )
            except Exception:
                logger.exception("Could not report early worker failure for %s", task_id)
        return False


def run_node_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(
        description="Execute one SimStack task through the runner API"
    )
    parser.add_argument(
        "--node-id",
        default=os.environ.get("SIMSTACK_TASK_ID"),
        help="Claimed task ID (or SIMSTACK_TASK_ID)",
    )
    parser.add_argument(
        "--claim-id",
        default=os.environ.get("SIMSTACK_TASK_CLAIM_ID"),
        help="Active claim ID (or SIMSTACK_TASK_CLAIM_ID)",
    )
    parser.add_argument(
        "--attempt",
        default=os.environ.get("SIMSTACK_TASK_ATTEMPT"),
        help="Active claim attempt (or SIMSTACK_TASK_ATTEMPT)",
    )
    parser.add_argument(
        "--resource",
        default=os.environ.get("SIMSTACK_RUNNER_RESOURCE"),
        help="Runner resource; must match the token/bootstrap resource",
    )
    parser.add_argument(
        "--project-root",
        default=os.environ.get("SIMSTACK_PROJECT_ROOT"),
        help="Local project checkout containing node implementations",
    )
    args = parser.parse_args()

    try:
        node_id = _required_value(args.node_id, name="SIMSTACK_TASK_ID/--node-id")
        claim_id = _required_value(
            args.claim_id, name="SIMSTACK_TASK_CLAIM_ID/--claim-id"
        )
        attempt = _required_attempt(args.attempt)
    except ValueError as exc:
        parser.error(str(exc))

    configure_worker_event_loop()
    succeeded = asyncio.run(
        run_node_from_id(
            node_id,
            args.resource,
            args.project_root,
            claim_id=claim_id,
            attempt=attempt,
        )
    )
    logging.shutdown()
    if not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    run_node_main()
