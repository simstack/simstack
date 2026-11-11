"""Register scheduler submissions and exchange Slurm observations with Server."""


class SlurmJobsAPI:
    def __init__(self, requests, tasks):
        self.requests = requests
        self.tasks = tasks

    async def scheduler_submission(self, entry, submission_id, *, phase="prepared"):
        from uuid import UUID, uuid5

        return await self.requests.command(
            "/scheduler/submissions",
            {
                "execution": self.tasks.execution(entry),
                "submission_id": submission_id,
                "phase": phase,
            },
            request_id=submission_id if phase == "prepared" else str(uuid5(UUID(submission_id), phase)),
        )

    async def scheduler_accepted(self, execution, submission_id, job_id):
        from uuid import UUID, uuid5

        return await self.requests.command(
            f"/scheduler/submissions/{submission_id}/accepted",
            {
                "execution": execution,
                "job_id": str(job_id),
            },
            request_id=str(uuid5(UUID(submission_id), "accepted")),
        )

    async def scheduler_watchlist(self):
        return await self.requests.read("/scheduler/watchlist")

    async def container_intent(self, entry, *, backend="docker"):
        from asyncio import sleep
        from uuid import uuid4
        from simstack.server_api.http_transport import RunnerAPIError
        request_id = str(uuid4())
        while True:
            try:
                return await self.requests.command("/scheduler/container-intent",
                    {"execution": self.tasks.execution(entry), "backend": backend}, request_id=request_id)
            except RunnerAPIError as exc:
                if exc.status_code is not None and exc.status_code < 500:
                    raise
                await sleep(.1)

    async def execution_control(self, entry):
        execution = self.tasks.execution(entry)
        return await self.requests.read(f"/scheduler/executions/{execution['task_id']}/control",
            **{key: value for key, value in execution.items() if key != "task_id"})

    async def start_incarnation(self, execution, submission_id, job_id, restart_count, process_id):
        from asyncio import sleep
        from uuid import uuid4
        from simstack.server_api.http_transport import RunnerAPIError
        from simstack.server_api.workflow_tasks import decode_task_response

        request_id = str(uuid4())
        while True:
            try:
                result = await self.requests.command(f"/scheduler/submissions/{submission_id}/start",
                    {"execution": execution, "job_id": job_id, "restart_count": restart_count,
                     "process_id": process_id}, request_id=request_id)
                if result["disposition"] == "waiting_for_stop":
                    # This command committed only a stop request, not admission.
                    # Retrying admission uses a new command after its old response
                    # has been reconciled, preserving lost-reply idempotency.
                    request_id = str(uuid4())
                    await sleep(.1)
                    continue
                return {**result, "task": decode_task_response(result["task"])}
            except RunnerAPIError as exc:
                if exc.status_code is not None and exc.status_code < 500:
                    raise
                # Retrying this precise command reconciles a lost admission
                # reply without admitting another batch process.
                await sleep(0.1)

    async def scheduler_observations(self, observations):
        return await self.requests.command(
            "/scheduler/observations", {"observations": observations}
        )
