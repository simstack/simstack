"""Execute a server-claimed self child inside the parent's inherited boundary.

The server decides ownership and supplies a child-only token. A separate worker
process keeps task cwd, logging, scopes and concurrent child state isolated; it
inherits the parent's operator-enforced OS network boundary without consuming a
supervisor slot or receiving its token.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from simstack.core.context import context
from simstack.runner_api import ClaimedTask, ExecutionRunnerClient
from simstack.runner_runtime.environment import (
    sanitized_runner_environment,
    worker_environment_allowlist_from_environment,
    worker_environment_allowlist_value,
)

from simstack.runner_runtime.processes import supervise_worker

from .errors import RunnerTaskExecutionError

logger = logging.getLogger(__name__)


async def _report_inline_failure(claim: ClaimedTask, exc: BaseException) -> None:
    # A parent token cannot mutate its child. Keep reporting under the
    # delegated child credential, including failures before worker bootstrap.
    try:
        client = ExecutionRunnerClient(server_url=context.runner_api.server_url, runner_token=claim.execution_token)
        await client.fail_task(
            str(claim.task["id"]), claim_id=claim.claim_id, attempt=claim.attempt,
            error=type(exc).__name__, message=str(exc),
        )
    except Exception:
        logger.exception("Could not acknowledge failed inline child %s", claim.task["id"])


async def execute_inline_child(claim: ClaimedTask) -> None:
    task_id = str(claim.task["id"])
    try:
        resource = str(context.config.resource)
        allowlist = worker_environment_allowlist_from_environment()
        environment = sanitized_runner_environment(
            {
                "SIMSTACK_SERVER_URL": context.runner_api.server_url,
                "SIMSTACK_RUNNER_TOKEN": claim.execution_token,
                "SIMSTACK_TASK_ID": task_id,
                "SIMSTACK_TASK_CLAIM_ID": claim.claim_id,
                "SIMSTACK_TASK_ATTEMPT": str(claim.attempt),
                "SIMSTACK_RUNNER_RESOURCE": resource,
                "SIMSTACK_PROJECT_ROOT": str(context.config.project_root),
                "SIMSTACK_RUNNER_WORKDIR": str(context.config.workdir),
                "SIMSTACK_WORKER_ENVIRONMENT_ALLOWLIST": worker_environment_allowlist_value(allowlist),
            },
            allowed_environment_names=allowlist,
            workflow_bootstrap=bool(claim.task.get("workflow_revision")),
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "simstack.core.run_node",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=os.name != "nt",
        )
        result = await supervise_worker(process)
        if result.returncode != 0:
            raise RunnerTaskExecutionError(
                f"Inline child {task_id} worker exited with code {result.returncode}"
            )
    except (Exception, asyncio.CancelledError) as exc:
        await asyncio.shield(_report_inline_failure(claim, exc))
        raise
