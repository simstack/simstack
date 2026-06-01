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
    result = subprocess.run(
        f"squeue -j {job_id}",
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def get_job_info(job_id: str, task_id: ObjectId, resource: Resource) -> SlurmInfo | None:
    """Get job information from SLURM queue using squeue"""
    try:
        stdout = run_squeue_for_job(job_id)
        #logger.info(f"task_id: {task_id} running squeue for job {job_id}: result: {stdout}")

        if not stdout or stdout.strip() == "":
            # after a while slurm will stop returning info for jobs that are no longer running
            return None

        lines = stdout.splitlines()
        #logger.info(f"task_id: {task_id} slurm info for job {job_id}: {lines}")
        if len(lines) < 2:
            return None
        # The first line is the header; the second line is the single info line

        info_line = lines[1].strip()
        if not info_line:
            return None
        # Split the single line into parts separated by whitespace
        parts = re.split(r"\s+", info_line)
        #logger.info(f"task_id: {task_id} slurm info for job {job_id}: {parts}")
        # Expected default squeue columns:
        # JOBID PARTITION NAME USER ST TIME NODES NODELIST(REASON)
        name = parts[2] if len(parts) > 2 else ""
        user = parts[3] if len(parts) > 3 else ""
        code = parts[4] if len(parts) > 4 else ""
        time_str = parts[5] if len(parts) > 5 else ""
        nodelist_raw = parts[7] if len(parts) > 7 else ""
        # Split nodelist on commas or whitespace, filter empties
        nodes = [n for n in re.split(r"[,\s]+", nodelist_raw) if n]

        slurm_info = SlurmInfo(
            node_registry=task_id,
            resource=resource,
            job_id=job_id,
            updated=datetime.now(),
            name=name,
            user=user,
            code=code,
            time=time_str,
            nodes=nodes,
        )

        return slurm_info
    except Exception as e:
        logger.exception(f"Error getting job info for {job_id}: {str(e)}")
        return None


async def clean_slurm_info(resource: Resource, user: str = None):
    """Clean up old slurm info entries"""
    try:
        squeue_cmd = "squeue"
        if user:
            squeue_cmd += f" -u {user}"

        if context.config.docker:
            watchdog_id = f"slurm_{uuid.uuid4()}"
            queue_dir = context.config.workdir / "queue"
            result = submit_to_watchdog(
                squeue_cmd, watchdog_id, queue_dir=queue_dir
            )
        else:
            result = subprocess.run(
                squeue_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )


        if result.returncode == 0:
            active_job_ids = set()
            for line in result.stdout.splitlines():
                parts = line.split()
                if not parts or parts[0] == "JOBID":
                    continue
                active_job_ids.add(parts[0])

            # Find all SLURM info entries for this resource
            # the user id is truncated on saving
            # if user:
            #     running_jobs = await context.db.engine.find(
            #         SlurmInfo, (SlurmInfo.resource.value == resource.value) & (SlurmInfo.user == user)
            #     )
            # else:
            running_jobs = await context.db.engine.find(SlurmInfo, SlurmInfo.resource.value == resource.value)
            # logger.info(f"Found {running_jobs} slurm info entries for {resource}")
            # logger.info(f"Active job IDs: {active_job_ids} Slurm info IDs: {[job.job_id for job in running_jobs]}")
            # logger.info(f"User: {user} resource: {resource} ")

            # Delete entries for jobs that are no longer running
            for job in running_jobs:
                if job.job_id not in active_job_ids:
                    await context.db.delete(job)
                    logger.info(f"Deleted SLURM info for completed job {job.job_id}")

    except Exception as e:
        logger.exception(f"Error cleaning slurm info for {resource}: {str(e)}")
