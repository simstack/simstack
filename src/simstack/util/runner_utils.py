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
    git_path_list = context.config.git_list + [context.config.project_root]
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
        result = run_squeue_for_job(job_id)
        logger.info(f"task_id: {task_id} running squeue for job {job_id}: result: {result}")
        if result.returncode == 0:
            if not result.stdout or result.stdout == "":
                return None
            lines = result.stdout.splitlines()
            logger.info(f"task_id: {task_id} slurm info for job {job_id}: {lines}")
            if len(lines) < 2:
                return None
            # The first line is the header; the second line is the single info line

            info_line = lines[1].strip()
            if not info_line:
                return None
            # Split the single line into parts separated by whitespace
            parts = re.split(r"\s+", info_line)
            logger.info(f"task_id: {task_id} slurm info for job {job_id}: {parts}")
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
        else:
            # after a while slurm will stop returning info for jobs that are no longer running
            # logger.error(f"Failed to get info for job {job_id}: {result.stderr}")
            return None
    except Exception as e:
        logger.exception(f"Error getting job info for {job_id}: {str(e)}")
        return None


async def clean_slurm_info(user: str, resource: Resource):
    """Clean up old slurm info entries"""
    try:
        if context.config.docker:
            watchdog_id = f"slurm_{uuid.uuid4()}"
            queue_dir = context.config.workdir / "queue"
            result = submit_to_watchdog(
                f"squeue -u {user}", watchdog_id, queue_dir=queue_dir
            )
        else:
            result = subprocess.run(
                f"squeue -u {user}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

        logger.info(f"Slurm Jobs {resource}: {result.stdout}")
        if result.returncode == 0:
            job_ids = [line.split()[0] for line in result.stdout.splitlines()]
            # logger.info(f"Cleaning up slurm info for {resource}: {result.stdout}")

            # Get list of running job IDs, skip the header line
            active_jobs = job_ids[1:] if len(job_ids) > 1 else []
            active_job_ids = set()
            for line in active_jobs:
                job_id = line.split()[0]
                active_job_ids.add(job_id)
            # Find all SLURM info entries for this resource
            running_jobs = await context.db.engine.find(
                SlurmInfo, SlurmInfo.resource == resource
            )
            # logger.info(f"Found {running_jobs} slurm info entries for {resource}")
            # logger.info(f"Active job IDs: {active_job_ids}")
            # Delete entries for jobs that are no longer running
            for job in running_jobs:
                if job.job_id not in active_job_ids:
                    await context.db.delete(job)
                    logger.info(f"Deleted SLURM info for completed job {job.job_id}")

    except Exception as e:
        logger.exception(f"Error cleaning slurm info for {resource}: {str(e)}")
