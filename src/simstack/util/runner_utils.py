import re
import subprocess
import uuid
from datetime import datetime
from typing import List

from odmantic import ObjectId
from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.slurm_info import SlurmInfo
from simstack.util.git_repository_status import get_git_status
from simstack.util.submit_to_watchdog import submit_to_watchdog

import logging

logger = logging.getLogger("runner_utils")

SQUEUE_TIMEOUT_SECONDS = 30


class SqueueQueryError(RuntimeError):
    """squeue did not return a usable listing (timeout, controller down, empty, or garbage)."""


def _squeue_label(job_id: str | None) -> str:
    return f" for job {job_id}" if job_id else ""


def _squeue_stdout_from_result(result, *, job_id: str | None = None) -> str:
    returncode = getattr(result, "returncode", None)
    stdout = getattr(result, "stdout", None) or ""
    stderr = (getattr(result, "stderr", None) or "").strip()
    label = _squeue_label(job_id)
    if returncode != 0:
        raise SqueueQueryError(
            f"squeue{label} failed with return code {returncode}"
            + (f": {stderr}" if stderr else "")
        )
    if not stdout.strip():
        raise SqueueQueryError(f"squeue{label} returned no output")
    return stdout


def _run_squeue_command(command: str, *, job_id: str | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SQUEUE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise SqueueQueryError(
            f"squeue{_squeue_label(job_id)} timed out after {SQUEUE_TIMEOUT_SECONDS}s"
        ) from exc
    return _squeue_stdout_from_result(result, job_id=job_id)


def _squeue_job_lines(stdout: str, *, job_id: str | None = None) -> list[str]:
    lines = stdout.splitlines()
    header = lines[0].strip() if lines else ""
    if not header.upper().startswith("JOBID"):
        raise SqueueQueryError(
            f"squeue{_squeue_label(job_id)} returned unexpected output: {header[:200]}"
        )
    return [line for line in lines[1:] if line.strip()]


def make_git_status_list() -> List[str]:
    git_status_list = []
    git_path_list = [context.config.project_root]
    for path in git_path_list:
        result = get_git_status(path)
        if result["branch"]:
            value = result["branch"] + "[" + result["short_hash"] + "]"
            if result["up_to_date"]:
                value += " (up-to-date)"
            else:
                value += " (behind " + str(result["behind"]) + " commits)"
            git_status_list.append(value)
        else:
            git_status_list.append("No branch found")
    return git_status_list


def run_squeue_for_job(job_id: str) -> str:
    return _run_squeue_command(f"squeue -j {job_id}", job_id=job_id)


def get_job_info(
    job_id: str, task_id: ObjectId, resource: Resource
) -> SlurmInfo | None:
    """Return SlurmInfo while the job is in the queue.

    Returns None only when squeue succeeded and the job is no longer listed.
    Raises SqueueQueryError when squeue itself failed (timeout, overload, empty
    or unexpected output) so callers do not treat a probe failure as completion.
    """
    stdout = run_squeue_for_job(job_id)
    job_lines = _squeue_job_lines(stdout, job_id=job_id)
    if not job_lines:
        return None
    parts = re.split(r"\s+", job_lines[0].strip())
    # Expected default squeue columns:
    # JOBID PARTITION NAME USER ST TIME NODES NODELIST(REASON)
    nodelist_raw = parts[7] if len(parts) > 7 else ""
    return SlurmInfo(
        node_registry=task_id,
        resource=resource,
        job_id=job_id,
        updated=datetime.now(),
        name=parts[2] if len(parts) > 2 else "",
        user=parts[3] if len(parts) > 3 else "",
        code=parts[4] if len(parts) > 4 else "",
        time=parts[5] if len(parts) > 5 else "",
        nodes=[n for n in re.split(r"[,\s]+", nodelist_raw) if n],
    )


async def clean_slurm_info(resource: Resource, user: str | None = None) -> None:
    """Clean up old slurm info entries"""
    try:
        squeue_cmd = "squeue"
        if user:
            squeue_cmd += f" -u {user}"

        if context.config.docker:
            watchdog_id = f"slurm_{uuid.uuid4()}"
            queue_dir = context.config.workdir / "queue"
            stdout = _squeue_stdout_from_result(
                submit_to_watchdog(squeue_cmd, watchdog_id, queue_dir=queue_dir)
            )
        else:
            stdout = _run_squeue_command(squeue_cmd)

        active_job_ids = {line.split()[0] for line in _squeue_job_lines(stdout) if line.split()}

        running_jobs = await context.db.find(
            SlurmInfo, SlurmInfo.resource.value == resource.value
        )
        for job in running_jobs:
            if job.job_id not in active_job_ids:
                await context.db.delete(job)
                logger.info(f"Deleted SLURM info for completed job {job.job_id}")

    except SqueueQueryError as e:
        logger.warning(f"Skipping slurm info cleanup for {resource}: {e}")
    except Exception as e:
        logger.exception(f"Error cleaning slurm info for {resource}: {str(e)}")
