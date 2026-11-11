"""Workflow task submission, execution transitions and attempt outcomes."""

from __future__ import annotations
from simstack.models.document.codec import decode_document_value, encode_document_value


def decode_task_response(value):
    from simstack.models.node_registry import NodeRegistry

    if value is None:
        return None
    if isinstance(value, NodeRegistry):
        return value
    value = decode_document_value(value)
    identity = {
        key: value.pop(key) for key in list(value) if key.startswith("_execution_")
    }
    entry = NodeRegistry.model_validate(value)
    for key, item in identity.items():
        object.__setattr__(entry, key, item)
    container = getattr(entry, "_execution_container_owner", None)
    if container and WorkflowTasksAPI.execution(entry) == container:
        # Parameters may be edited for the next generation. This admitted
        # execution must still use its original physical backend on recovery.
        entry.parameters = entry.parameters.model_copy(update={"queue": "default", "in_docker": True})
    return entry


class WorkflowTasksAPI:
    def __init__(self, requests, scientific_values):
        self.requests = requests
        self.scientific_values = scientific_values

    @staticmethod
    def execution(entry):
        execution = {
            "task_id": str(entry.id),
            "generation": getattr(entry, "_execution_generation", None),
            "claim_id": getattr(entry, "_execution_claim_id", None),
            "start_id": getattr(entry, "_execution_start_id", None),
        }
        owner = getattr(entry, "_execution_owner", None)
        if owner and all(owner["execution"].get(key) == execution.get(key) for key in ("task_id", "generation", "claim_id")):
            execution["start_id"] = execution["start_id"] or owner["execution"]["start_id"]
        container = getattr(entry, "_execution_container_owner", None)
        if container and all(container.get(key) == execution.get(key) for key in ("task_id", "generation", "claim_id")):
            execution["start_id"] = execution["start_id"] or container["start_id"]
        return execution

    async def get_task(self, task_id):
        result = await self.requests.read(f"/tasks/{task_id}")
        entry = decode_task_response(result.get("task"))
        if entry is not None:
            await self.scientific_values.materialize_references([entry])
        return entry

    async def get_outcome(self, entry, *, generation_only=False):
        result = await self.requests.read(
            f"/tasks/{entry.id}/outcome",
            **{
                key: value
                for key, value in self.execution(entry).items()
                if key == "generation" or not generation_only and key != "task_id"
            },
        )
        if result.get("task") is not None:
            current = decode_task_response(result["task"])
            for name in ("phase", "exit_code", "signal", "diagnostics"):
                if name in (result.get("outcome") or {}):
                    setattr(current, name, result["outcome"][name])
            await self.scientific_values.materialize_references([current])
            return current
        if result.get("outcome") is not None:
            payload = entry.model_dump(mode="python")
            for name, value in decode_document_value(result["outcome"]).items():
                if name in type(entry).model_fields:
                    payload[name] = value
            payload.update(
                {
                    key: value
                    for key, value in vars(entry).items()
                    if key.startswith("_execution_")
                }
            )
            payload["_execution_native_controller"] = result["outcome"].get("_execution_native_controller", False)
            if generation_only:
                payload.update({"_execution_" + key: value for key, value in result["outcome"].get("execution", {}).items()
                    if key != "task_id"})
            snapshot = decode_task_response(payload)
            await self.scientific_values.materialize_references([snapshot])
            return snapshot
        return None

    async def task_inputs(self, task_id):
        return await self.requests.read(f"/tasks/{task_id}/inputs")

    async def _load_invocation_result(self, result):
        entry = decode_task_response(result.get("task"))
        if entry is not None:
            await self.scientific_values.materialize_references([entry])
        return {**result, "task": entry}

    async def create_child(self, payload, *, request_id):
        result = await self.requests.command(
            f"/tasks/{payload['parent_execution']['task_id']}/children",
            payload,
            request_id=request_id,
        )
        return await self._load_invocation_result(result)

    async def resolve_invocation(self, payload, *, request_id):
        return await self.requests.command("/task-routes/resolve", payload, request_id=request_id)

    async def reuse_invocation(self, payload, *, request_id):
        result = await self.requests.command("/task-cache/reuse", payload, request_id=request_id)
        return await self._load_invocation_result(result)

    async def acquire_work(self, capacity=1, *, request_id):
        if self.requests.authorization != "runner":
            raise ValueError("User SDK clients cannot acquire Runner work")
        result = await self.requests.command(
            "/work/acquire", {"capacity": capacity}, request_id=request_id
        )
        return [decode_task_response(item) for item in result.get("tasks", [])]

    async def reconcile_attempts(self, *, claims, outcomes, request_id=None):
        result = await self.requests.command(
            "/attempts/reconcile",
            {
                "claims": claims,
                "outcomes": outcomes,
            },
            request_id=request_id,
        )
        return {
            **result,
            "claims": [
                {**item, "task": decode_task_response(item["task"])}
                if item.get("task")
                else item
                for item in result.get("claims", [])
            ],
        }

    async def recover_local_launch(self, claim):
        result = await self.requests.command("/attempts/recover-local", {
            "execution": {key: claim.get(key) for key in ("task_id", "generation", "claim_id", "start_id")},
        })
        return {**result, "task": decode_task_response(result.get("task"))}

    async def recover_local_owner(self, claim, *, container_stopped=False, native_stopped=False, exit_code=None):
        import json
        from uuid import NAMESPACE_URL, uuid5
        execution = {key: claim.get(key) for key in ("task_id", "generation", "claim_id", "start_id")}
        body = {
            "execution": execution, "process_id": claim["process_id"],
        }
        identity = execution
        if container_stopped:
            body.update(container_stopped=True, exit_code=exit_code)
            identity = {"execution": execution, "container_stopped": True, "exit_code": exit_code}
        if native_stopped:
            body["native_stopped"] = True
            identity = {"execution": execution, "native_stopped": True}
        request_id = str(uuid5(NAMESPACE_URL, "recover-local-owner:" + json.dumps(identity, sort_keys=True)))
        result = await self.requests.command("/attempts/recover-local-owner", body, request_id=request_id)
        return {**result, "task": decode_task_response(result.get("task"))}

    async def get_request(self, request_id):
        response = await self.requests.read(f"/requests/{request_id}")
        result = response.get("result")
        if result and result.get("task"):
            response = {
                **response,
                "result": {**result, "task": decode_task_response(result["task"])},
            }
        return response

    async def admit_container(self, entry):
        import json
        from uuid import NAMESPACE_URL, uuid5
        execution = self.execution(entry)
        result = await self.requests.command("/attempts/container-intent",
            {"execution": execution, "backend": "docker"},
            request_id=str(uuid5(NAMESPACE_URL, "container-intent:" + json.dumps(execution, sort_keys=True))))
        return decode_task_response(result.get("task"))

    async def start_attempt(self, entry, process_id=None):
        execution = self.execution(entry)
        import os
        from asyncio import sleep
        from simstack.server_api.http_transport import RunnerAPIError

        body = {"execution": execution, "process_id": process_id}
        # Only this controller's own science carries this exact environment;
        # inline children and container workers do not acquire native ownership.
        if (process_id is not None and os.environ.get("SIMSTACK_SCIENCE_CONTROLLER_PID") == str(process_id)
                and all(os.environ.get("SIMSTACK_EXECUTION_" + name) == execution.get(field)
                        for name, field in (("TASK", "task_id"), ("GENERATION", "generation"),
                                            ("CLAIM", "claim_id"), ("START", "start_id")))):
            body["native_controller"] = True
        # A lost response cannot decide whether science may run. Reconcile the
        # original command, then replay only that same idempotent identity.
        while True:
            try:
                result = await self.requests.command(
                    "/attempts/start",
                    body,
                    request_id=execution["start_id"],
                )
                return decode_task_response(result.get("task"))
            except RunnerAPIError as exc:
                if exc.status_code is not None and exc.status_code < 500:
                    raise
                try:
                    known = await self.get_request(execution["start_id"])
                    if known["state"] == "applied":
                        return decode_task_response(known["result"].get("task"))
                    if known["state"] == "rejected":
                        raise RunnerAPIError(
                            str(known.get("detail") or "Start attempt was rejected"),
                            status_code=known.get("status_code", 409),
                        ) from exc
                except RunnerAPIError as read_error:
                    if read_error.status_code is not None and read_error.status_code < 500:
                        raise
                await sleep(0.1)

    async def record_process(self, entry, pid, *, host_owner=False, container_backend=None):
        body = {"execution": self.execution(entry), "process_id": pid}
        if host_owner:
            body["host_owner"] = True
        if container_backend is not None:
            body["container_backend"] = container_backend
        result = await self.requests.command(
            "/attempts/process", body
        )
        return decode_task_response(result.get("task"))

    async def complete_attempt(self, entry, outputs=None, return_kind=None):
        from types import SimpleNamespace
        from simstack.runner_runtime.pending_cancellations import drain_cancellations
        await drain_cancellations(SimpleNamespace(requests=self.requests, tasks=self), self.execution(entry))
        body = {
            "execution": self.execution(entry),
            "results_references": encode_document_value(
                outputs if outputs is not None else entry.results_references
            ),
            "output_publication": getattr(entry, "_output_publication", None),
            "return_kind": return_kind or entry.return_kind,
            "info_files": encode_document_value(entry.info_files),
            "artifact_ids": encode_document_value(entry.artifact_ids),
            "message": entry.message,
        }
        if hasattr(entry, "_outcome_custom_name"):
            body.update(
                custom_name=entry._outcome_custom_name,
                expected_custom_name=getattr(entry, "_execution_custom_name", None),
            )
        result = await self.requests.command("/attempts/complete", body)
        return decode_task_response(result.get("task"))

    async def fail_attempt(
        self, entry, error, phase="execute", diagnostics=None, *, include_outputs=False,
        exit_code=None, signal=None, return_kind="exception",
    ):
        body = {
            "execution": self.execution(entry),
            "error": error,
            "phase": phase,
            "diagnostics": diagnostics or [],
            "exit_code": exit_code,
            "signal": signal,
            "return_kind": return_kind,
        }
        if include_outputs:
            body.update(
                {
                    "results_references": encode_document_value(
                        entry.results_references
                    ),
                    "output_publication": getattr(entry, "_output_publication", None),
                    "info_files": encode_document_value(entry.info_files),
                    "artifact_ids": encode_document_value(entry.artifact_ids),
                    "message": entry.message,
                }
            )
        result = await self.requests.command("/attempts/fail", body)
        return decode_task_response(result.get("task"))

    async def handoff(self, entry, resource, in_docker):
        result = await self.requests.command(
            "/attempts/handoff",
            {
                "execution": self.execution(entry),
                "resource": str(resource),
                "in_docker": in_docker,
            },
        )
        return decode_task_response(result.get("task"))

    async def terminating_tasks(self):
        result = await self.requests.read("/work/terminating")
        return [decode_task_response(item) for item in result.get("tasks", [])]

    async def children(self, parent):
        task_id = getattr(parent, "id", parent)
        result = await self.requests.read(f"/tasks/{task_id}/children")
        return [decode_task_response(item) for item in result.get("tasks", [])]

    async def cancel_child(self, parent_id, parent_execution, child, error, *, request_id=None):
        return await self.cancel_dependency(parent_id, parent_execution,
            str(getattr(child, "id", child)), getattr(child, "_execution_generation", None),
            error, request_id=request_id)

    async def cancel_dependency(self, parent_id, parent_execution, child_id, generation, error, *, request_id=None):
        if parent_execution["task_id"] != str(parent_id):
            raise ValueError("Parent execution differs from its task identity")
        result = await self.requests.command(
            f"/tasks/{parent_id}/children/cancel",
            {"parent_execution": parent_execution, "child_ids": [child_id],
             "child_generations": {child_id: generation}, "reason": error},
            request_id=request_id,
        )
        return decode_task_response(result.get("task"))

    async def termination_confirmed(self, entry, error):
        result = await self.requests.command(
            "/attempts/termination-confirmed",
            {"execution": self.execution(entry), "reason": error},
        )
        return decode_task_response(result.get("task"))

    async def request_cancellation(self, entry, error):
        result = await self.requests.command("/attempts/cancel",
            {"execution": self.execution(entry), "reason": error})
        return decode_task_response(result.get("task"))

    async def submit_root(self, payload, *, request_id):
        if self.requests.authorization != "user":
            raise ValueError(
                "Root submission requires an explicit user SDK client and user token"
            )
        result = await self.requests.command(
            "/root-submissions", payload, request_id=request_id
        )
        return await self._load_invocation_result(result)
