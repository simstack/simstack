from __future__ import annotations

import asyncio
import logging
import platform
import subprocess

from simstack.core.context import context
from simstack.models import NodeRegistry
from simstack.runner_api import ExecutionRunnerClient
from simstack.runner_protocol import MIN_RUNNER_LEASE_SECONDS
from simstack.runner_runtime.container_launch import container_launch
from simstack.runner_runtime.processes import (
    sanitize_worker_diagnostic, supervise_worker, worker_failure_diagnostic,
)


logger = logging.getLogger("DockerRunner")


async def run_docker(
    registry_entry: NodeRegistry,
    *,
    claim_id: str,
    attempt: int,
    lease_seconds: int,
    runner_api: ExecutionRunnerClient,
) -> bool:
    """Run a claimed task in a container with API credentials only."""

    if isinstance(lease_seconds, bool) or lease_seconds < MIN_RUNNER_LEASE_SECONDS:
        raise ValueError(
            "Container worker lease_seconds must satisfy the runner protocol minimum"
        )
    task_id = str(registry_entry.id)
    cmd, child_environment = container_launch(
        registry_entry, claim_id=claim_id, attempt=attempt, runner_api=runner_api,
        config=context.config, execution_adapter=context.runner_adapter,
    )

    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=platform.system() != "Windows",
        env=child_environment,
    )
    async def renew_lease() -> object:
        return await runner_api.renew_lease(
            task_id, claim_id=claim_id, attempt=attempt, lease_seconds=lease_seconds,
        )

    result = await supervise_worker(
        process, renew_lease=renew_lease, renew_every=max(10.0, lease_seconds / 3),
    )
    secrets = (runner_api.runner_token,)
    if result.returncode != 0:
        diagnostic = worker_failure_diagnostic(result.stdout, result.stderr, secrets=secrets)
        raise RuntimeError(
            f"Container worker exited with code {result.returncode}"
            + (f". Bounded worker diagnostic:\n{diagnostic}" if diagnostic else "")
        )
    stdout = sanitize_worker_diagnostic(result.stdout, secrets=secrets)
    stderr = sanitize_worker_diagnostic(result.stderr, secrets=secrets)
    if stdout:
        logger.info("Container output for task_id=%s: %s", task_id, stdout)
    if stderr:
        logger.warning("Container stderr for task_id=%s: %s", task_id, stderr)
    return True
