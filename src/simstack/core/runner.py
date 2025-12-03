import argparse
import asyncio
import logging
import os
import re
import socket
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_database
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerType, RunnerEventEnum
from simstack.models.slurm_info import SlurmInfo
from simstack.util.git_repository_status import get_git_status
from simstack.util.submit_to_watchdog import submit_to_watchdog

logger = logging.getLogger("NodeRunner")


async def run_node(registry_entry: NodeRegistry):
    """Run a single node by its ID from the database"""
    if isinstance(context.config.resource, str):
        resource = Resource(value=context.config.resource)
    else:
        resource = context.config.resource
    pid = os.getpid()
    username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    hostname = socket.gethostname()

    runner_event = RunnerEvent(
        runner_type=RunnerType.NODE_RUNNER,
        event=RunnerEventEnum.NODE_STARTED,
        pid=pid,
        hostname=hostname,
        user=username,
        resource=resource,
        node_id=registry_entry.id,
    )
    await context.db.save(runner_event)

    try:
        logger.info(
            f"Running node task_id: {registry_entry.id} on resource {context.config.resource}"
        )
        if (
            hasattr(registry_entry.parameters, "queue")
            and registry_entry.parameters.queue == "slurm-queue"
        ):
            await submit_node(registry_entry)
        else:
            # Create the node from the registry entry
            node = await node_from_database(registry_entry)
            if not node:
                logger.error(
                    f"Failed to create node from registry entry task_id: {registry_entry.id} on resource {context.config.resource}"
                )
                registry_entry.status = TaskStatus.FAILED
                await context.db.save(registry_entry)
                return False
            registry_entry = node.registry_entry # it may have changed
            if node.status == TaskStatus.SUBMITTED or node.status == TaskStatus.SLURM_QUEUED or node.status == TaskStatus.SLURM_QUEUED:
                await node.execute_node_locally()
            else:
                logger.info(
                    f"task_id: {registry_entry.id} skipping task: {registry_entry.name} with status {registry_entry.status}")

            return node.status == TaskStatus.COMPLETED
    except Exception as e:
        logger.exception(
            f"Error running node task_id: {registry_entry.id} on resource {context.config.resource} : {str(e)}"
        )
        if registry_entry:
            registry_entry.status = TaskStatus.FAILED
            await context.db.save(registry_entry)
        return False


def make_git_list() -> List[str]:
    git_list = []
    for path in context.config.git:
        result = get_git_status(path)
        if result["branch"]:
            value = result["branch"] + "[" + result["short_hash"] + "]"
            if result["up_to_date"]:
                value += " (up-to-date)"
            else:
                value += " (behind " + str(result["behind"]) + " commits)"
            git_list.append(value)
        else:
            git_list.append("No branch found")
    return git_list


def run_squeue_for_job(job_id: str) -> str:
    if context.config.docker:
        result = subprocess.run(
            f"squeue -j {job_id}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    else:
        watchdog_id = f"slurm_{uuid.uuid4()}"
        queue_dir = context.config.workdir / "queue"
        result = submit_to_watchdog(
            f"squeue -j {job_id}", watchdog_id, queue_dir=queue_dir
        )
    return result.stdout


def get_job_info(job_id: str, task_id: ObjectId, resource: str) -> SlurmInfo | None:
    """Get job information from SLURM queue using squeue"""
    try:
        result = run_squeue_for_job(job_id)
        if result.returncode == 0:
            if not result.stdout or result.stdout == "":
                return None
            lines = result.stdout.splitlines()
            logger.info(f"slurm info for job {job_id}: {lines}")
            if len(lines) < 2:
                return None
            # The first line is the header; the second line is the single info line

            info_line = lines[1].strip()
            if not info_line:
                return None
            # Split the single line into parts separated by whitespace
            parts = re.split(r"\s+", info_line)
            logger.info(f"slurm info for job {job_id}: {parts}")
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


async def run_nodes_for_resource(
    resource_name, polling_interval=5, restart_minutes=None, max_concurrent=10
) -> str:
    """Continuously poll for and run nodes assigned to a specific resource"""
    pid = os.getpid()
    username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    hostname = socket.gethostname()
    resource = Resource(value=resource_name)
    logger.info(
        f"Starting node runner for resource: {resource_name} by user: {username}"
    )
    runner_event = RunnerEvent(
        runner_type=RunnerType.RESOURCE_RUNNER,
        event=RunnerEventEnum.RUNNER_STARTED,
        resource=resource,
        user=username,
        hostname=hostname,
        pid=pid,
        git_status=make_git_list(),
    )
    await context.db.save(runner_event)

    # Setup restart timer if needed
    # if restart_minutes:
    #     restart_task = asyncio.create_task(schedule_restart(resource_name, restart_minutes))

    # Create a semaphore to limit concurrency
    semaphore = asyncio.Semaphore(max_concurrent)
    running_tasks = set()

    async def run_node_with_semaphore(registry_entry):
        async with semaphore:
            return await run_node(registry_entry)

    try:
        count = 0
        while True:
            try:
                # Clean up the completed tasks
                completed_tasks = {task for task in running_tasks if task.done()}
                for task in completed_tasks:
                    try:
                        await task
                    except Exception as e:
                        logger.exception(f"Task completed with error: {e}")
                    running_tasks.remove(task)

                # Check for STOP file in python path
                for path in context.config.python_path:
                    stop_file = Path(path) / "STOP"
                    if stop_file.exists():
                        runner_event = RunnerEvent(
                            runner_type=RunnerType.RESOURCE_RUNNER,
                            event=RunnerEventEnum.SHUTDOWN,
                            hostname=hostname,
                            user=username,
                            pid=pid,
                            resource=resource,
                            message="STOP FILE FOUND",
                        )
                        await context.db.save(runner_event)
                        logger.info(f"STOP file found at {stop_file}, exiting...")
                        return "stop"

                # Load tasks that are waiting for this resource
                registry_entry_list = await context.db.load_waiting_tasks_for_resource(
                    resource_name
                )
                if registry_entry_list:
                    logger.info(
                        f"Retrieved {len(registry_entry_list)} tasks for resource {resource_name}"
                    )

                    for registry_entry in registry_entry_list:
                        logger.info(
                            f"Task task_id: {registry_entry.id} will be run on {resource_name}"
                        )
                        # Create the task with semaphore - don't await
                        task = asyncio.create_task(
                            run_node_with_semaphore(registry_entry)
                        )
                        running_tasks.add(task)

                runner_event = await context.db.find_one(
                    RunnerEvent,
                    RunnerEvent.runner_type == RunnerType.RESOURCE_RUNNER
                    and RunnerEvent.resource == resource
                    and RunnerEvent.event == RunnerEventEnum.ALIVE
                    and RunnerEvent.pid == pid,
                )
                message = f"{count*polling_interval:6d}s"
                if runner_event:
                    runner_event.message = message
                    runner_event.timestamp = datetime.now()
                    runner_event.event = RunnerEventEnum.ALIVE
                    runner_event.git_status = make_git_list()

                else:
                    runner_event = RunnerEvent(
                        runner_type=RunnerType.RESOURCE_RUNNER,
                        event=RunnerEventEnum.ALIVE,
                        resource=resource,
                        user=username,
                        hostname=hostname,
                        pid=pid,
                        message=message,
                        git_status=make_git_list(),
                    )
                await context.db.save(runner_event)

                running_jobs = await context.db.engine.find(
                    NodeRegistry,
                    (NodeRegistry.status == TaskStatus.RUNNING)
                    & (NodeRegistry.parameters.resource == resource),
                )
                for job in running_jobs:
                    if job.job_id is not None:
                        slurm_info = get_job_info(
                            job.job_id, job.id, Resource(value=resource_name)
                        )
                        slurm_entry = await context.db.find_one(
                            SlurmInfo, SlurmInfo.job_id == job.job_id
                        )
                        logger.info(
                            f"Found running job {job} with slurm info: {slurm_info} {slurm_entry is not None}"
                        )

                        if slurm_info:
                            if slurm_entry:
                                slurm_entry.code = slurm_info.code
                                slurm_entry.time = slurm_info.time
                                slurm_entry.updated = datetime.now()
                                await context.db.save(slurm_entry)
                            else:
                                await context.db.save(slurm_info)
                        else:
                            # if the job had started but is no longer running, delete the entry from the database
                            await asyncio.sleep(polling_interval)
                            check_job = await context.db.engine.find_one(
                                NodeRegistry, NodeRegistry.id == job.id
                            )
                            logger.warning(
                                f"Job task_id: {job.id} {job.id} is no longer running {check_job.status}"
                            )
                            if slurm_entry:
                                await context.db.delete(slurm_entry)
                            if slurm_entry and check_job.status == TaskStatus.RUNNING:
                                job.job_id = None
                                job.status = TaskStatus.TIME_OUT
                                await context.db.save(job)

                await clean_slurm_info(username, resource)

                await asyncio.sleep(polling_interval)
                count = count + 1
                if restart_minutes and count > restart_minutes:
                    runner_event = RunnerEvent(
                        runner_type=RunnerType.RESOURCE_RUNNER,
                        event=RunnerEventEnum.RESTART,
                        user=username,
                        hostname=hostname,
                        pid=pid,
                        resource=Resource(value=resource_name),
                        message="restart by count",
                        git_status=make_git_list(),
                    )
                    await context.db.save(runner_event)
                    return "restart"
            except Exception as e:
                logger.exception(f"Error in run_nodes_for_resource: {str(e)}")
                if restart_minutes and count > restart_minutes:
                    return "restart"
                await asyncio.sleep(polling_interval)
    except asyncio.CancelledError:
        logger.info("Node runner task was cancelled")
        raise


async def async_main(args):
    """Async entry point"""
    context.initialize(resource=args.resource, db_name=args.db_name)

    if args.resource:
        context.config.resource = Resource(value=args.resource)
        logger.info(f"Setting resource for runner to {args.resource}")
        await run_nodes_for_resource(args.resource, args.polling_interval, None)


def runner_main():
    parser = argparse.ArgumentParser(description="Run nodes for a specific resource")
    parser.add_argument(
        "--resource",
        type=str,
        default="local",
        help="Resource name to process tasks for",
    )

    parser.add_argument(
        "--db-name",
        type=str,
        help="Specify a non-standard database",
    )

    parser.add_argument(
        "--polling-interval",
        type=int,
        default=20,
        help="Interval in seconds between polling for new tasks",
    )

    args = parser.parse_args()

    # Run the async main function
    asyncio.run(async_main(args))
    pid = os.getpid()
    logger.info(f"runner with pid {pid} shutting down normally")


if __name__ == "__main__":
    runner_main()
