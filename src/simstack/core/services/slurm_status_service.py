import logging
from datetime import datetime

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry
from simstack.models.parameters import Resource
from simstack.models.slurm_info import SlurmInfo
from simstack.util.runner_utils import get_job_info, clean_slurm_info
from simstack.core.services.base_service import BaseService

logger = logging.getLogger("NodeRunner")

class SlurmStatusService(BaseService):
    def __init__(self, resource: Resource, interval):
        super().__init__("SlurmStatus", resource, interval)
        self._resource_name = str(resource)

    async def execute(self):
        try:
            # logger.info(f"Running clean_slurm_info for {self._resource} and user {self._username}")
            await clean_slurm_info(self._resource, user=self._username)

            running_tasks = await context.db.engine.find(
                NodeRegistry,
                (NodeRegistry.status == TaskStatus.RUNNING)
                & (NodeRegistry.parameters.resource == self._resource),
            )
            queued_tasks = await context.db.engine.find(
                NodeRegistry,
                (NodeRegistry.status == TaskStatus.SLURM_QUEUED)
                & (NodeRegistry.parameters.resource == self._resource),
            )
            #logger.info(f"Checking Slurm status for {len(running_tasks)} running jobs on resource {self._resource}")

            for task in list(running_tasks) + list(queued_tasks):
                if task.job_id is not None:
                    logger.info(f"Checking Slurm status for task_id: {task.id} with job_id: {task.job_id}")
                    slurm_info = get_job_info(task.job_id, task.id, Resource(value=self._resource_name))
                    logger.info(f"Slurm status for task_id: {task.id}: {task.job_id} {slurm_info}")
                    slurm_entry = await context.db.find_one(SlurmInfo, SlurmInfo.job_id == task.job_id)

                    if slurm_info:
                        if slurm_entry:
                            slurm_entry.code = slurm_info.code
                            slurm_entry.time = slurm_info.time
                            slurm_entry.updated = datetime.now()
                            await context.db.save(slurm_entry)
                        else:
                            await context.db.save(slurm_info)
                    else:
                        # Logic for finished/timed out jobs
                        check_job = await context.db.engine.find_one(NodeRegistry, NodeRegistry.id == task.id)
                        if slurm_entry:
                            await context.db.delete(slurm_entry)
                        if check_job.status == TaskStatus.RUNNING:
                            task.job_id = None
                            task.status = TaskStatus.TIME_OUT
                            await context.db.save(task)

        except Exception as e:
            logger.exception(f"Error checking Slurm status: {e}")
