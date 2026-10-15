import json
import subprocess
from datetime import UTC, datetime

from simstack.models.document import ObjectId
from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.slurm_info import SlurmInfo

import logging

logger = logging.getLogger("RunnerSlurm")


_SLURM_STATUS_KEYS = {"present", "name", "user", "code", "time", "nodes"}


def run_squeue_for_job(job_id: str) -> dict[str, object]:
    if not isinstance(job_id, str) or not job_id.isascii() or not job_id.isdigit():
        raise ValueError("Slurm job id must contain only ASCII digits")
    command = list(context.runner_adapter.slurm_status().command_for(job_id))

    from simstack.runner_runtime.environment import sanitized_trusted_helper_environment

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=sanitized_trusted_helper_environment(),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Slurm status command failed with code {result.returncode}: {result.stderr}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Slurm status command returned invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _SLURM_STATUS_KEYS:
        raise RuntimeError(
            "Slurm status command must return exactly present, name, user, "
            "code, time, nodes"
        )
    if not isinstance(payload["present"], bool):
        raise RuntimeError("Slurm status field present must be a boolean")
    for name in ("name", "user", "code", "time"):
        if not isinstance(payload[name], str):
            raise RuntimeError(f"Slurm status field {name} must be a string")
    nodes = payload["nodes"]
    if not isinstance(nodes, list) or not all(isinstance(node, str) for node in nodes):
        raise RuntimeError("Slurm status field nodes must be a list of strings")
    return payload


def get_job_info(
    job_id: str, task_id: ObjectId, resource: Resource
) -> SlurmInfo | None:
    """Get job information from SLURM queue using squeue"""
    payload = run_squeue_for_job(job_id)
    if not payload["present"]:
        return None

    slurm_info = SlurmInfo(
        node_registry=task_id,
        resource=resource,
        job_id=job_id,
        updated=datetime.now(UTC),
        name=payload["name"],
        user=payload["user"],
        code=payload["code"],
        time=payload["time"],
        nodes=payload["nodes"],
    )

    return slurm_info
