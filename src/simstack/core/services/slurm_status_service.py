"""Observe Slurm facts; Server owns reconciliation and task transitions."""

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from simstack.core.context import context
from simstack.core.services.base_service import BaseService
from simstack.util.slurm_observation import observe_job, find_submission
from simstack.runner_runtime.pending_execution_failures import reconcile_failures
from simstack.server_api.http_transport import RunnerAPIError

logger = logging.getLogger("NodeRunner")


async def resource_uses_slurm_queue(resource):
    definition = await context.server.resource.resource_definition()
    return (
        definition.resource_str == str(resource) and definition.queue == "slurm-queue"
    )


class SlurmStatusService(BaseService):
    def __init__(self, resource, interval):
        super().__init__("SlurmStatus", resource, interval)

    async def execute(self):
        await reconcile_failures(context.server, resource=self._resource)
        watchlist = await context.server.slurm.scheduler_watchlist()
        from simstack.runner_runtime.slurm_submission_intents import (
            forget_intent, pending_intents, read_intent, submitter_alive,
        )
        watched = {item["execution"]["task_id"] for item in watchlist["submissions"]}
        for intent in list(pending_intents(context.server, self._resource)):
            identity = intent["execution"]
            if intent["phase"] == "accepted":
                try:
                    await context.server.slurm.scheduler_accepted(identity, intent["submission_id"], intent["job_id"])
                except RunnerAPIError as exc:
                    if exc.status_code == 404:
                        logger.warning("Retaining Slurm job %s for deleted task %s", intent["job_id"], identity["task_id"])
                        continue
                    if exc.status_code != 409:
                        raise
                forget_intent(context.server, intent)
            elif identity["task_id"] not in watched:
                try:
                    entry = await context.server.tasks.get_task(identity["task_id"])
                except RunnerAPIError as exc:
                    if exc.status_code != 404:
                        raise
                    entry = None
                if entry is None:
                    if intent["phase"] == "prepared":
                        forget_intent(context.server, intent)
                    else:
                        logger.warning("Retaining unresolved Slurm submission %s for deleted task %s", intent["submission_id"], identity["task_id"])
                    continue
                if context.server.tasks.execution(entry) != identity or entry.status in {"failed", "timeout", "completed"}:
                    forget_intent(context.server, intent)
        observations = []
        for item in watchlist["submissions"]:
            job_id = item["job_id"]
            if not job_id:
                reason = "No conclusive scheduler evidence for the submission"
                absent = False
                try:
                    job_id = await asyncio.to_thread(
                        find_submission, item["submission_id"], item["submitted_at"]
                    )
                    absent = job_id is None
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    reason = str(exc)[:500]
                    logger.warning(
                        "Could not reconcile Slurm submission %s: %s",
                        item["submission_id"],
                        exc,
                    )
                if job_id:
                    await context.server.slurm.scheduler_accepted(
                        item["submission_execution"], item["submission_id"], job_id
                    )
                    intent = read_intent(context.server, item["execution"]["task_id"])
                    if intent is not None and intent["execution"] == item["execution"]:
                        forget_intent(context.server, intent)
                else:
                    intent = read_intent(context.server, item["execution"]["task_id"])
                    owner_alive = None
                    if intent is not None and intent["execution"] == item["execution"]:
                        owner_alive = await asyncio.to_thread(submitter_alive, intent)
                    observations.append(
                        {
                            "execution": item["execution"],
                            "submission_id": item["submission_id"],
                            "job_id": None,
                            "kind": "unknown",
                            "source": "scheduler",
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                            "reason": reason,
                            "submission_absent": absent,
                            "submitter_alive": owner_alive,
                        }
                    )
                    continue
            observation = await asyncio.to_thread(observe_job, job_id,
                **({"restart_count": item["restart_count"]} if item.get("restart_count") is not None else {}))
            observations.append(
                {
                    **observation,
                    "execution": item["execution"],
                    "submission_id": item["submission_id"],
                }
            )
        if observations:
            await context.server.slurm.scheduler_observations(observations)
