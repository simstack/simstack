import argparse
import asyncio
import logging
import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path
import hashlib
import sys
import platform

from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_database
from simstack.core.submit_node import submit_node
from simstack.models import NodeRegistry
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerType, RunnerEventEnum
from simstack.models.slurm_info import SlurmInfo
from simstack.util.runner_utils import make_git_list, get_job_info, clean_slurm_info

logger = logging.getLogger("NodeRunner")

async def run_node_from_registry(registry_entry: NodeRegistry):
    # Create the node from the registry entry
    node = await node_from_database(registry_entry)
    if not node:
        logger.error(
            f"Failed to create node from registry entry task_id: {registry_entry.id} on resource {context.config.resource}")
        registry_entry.status = TaskStatus.FAILED
        await context.db.save(registry_entry)
        return False
    registry_entry = node.registry_entry  # it may have changed
    if node.status == TaskStatus.SUBMITTED or node.status == TaskStatus.SLURM_QUEUED or node.status == TaskStatus.SLURM_QUEUED:
        await node.execute_node_locally()
    else:
        logger.info(
            f"task_id: {registry_entry.id} skipping task: {registry_entry.name} with status {registry_entry.status}")

    return node.status == TaskStatus.COMPLETED

class RunnerManager:
    def __init__(self, resource: Resource):
        self.resource = resource
        self.pid = os.getpid()
        self.username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        self.hostname = socket.gethostname()
        self.time_started = datetime.now()
        self.services = []

    async def stop_all_services(self):
        """Gracefully stop all registered services"""
        logger.info("Stopping all services...")
        await asyncio.gather(*(s.stop() for s in self.services), return_exceptions=True)

    def _get_uptime_string(self) -> str:
        time_diff = datetime.now() - self.time_started
        days = time_diff.days
        hours, remainder = divmod(time_diff.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m {seconds}s"

    async def write_node_event(self, event: RunnerEventEnum, node_id: ObjectId, message: str = None):
        runner_event = RunnerEvent(
            runner_type=RunnerType.NODE_RUNNER,
            event=event,
            pid=self.pid,
            hostname=self.hostname,
            user=self.username,
            resource=self.resource,
            node_id=node_id,
            message=message,
        )
        await context.db.save(runner_event)

    async def write_resource_event(self, event: RunnerEventEnum, message: str = None):
        if event == RunnerEventEnum.ALIVE:

            time_diff = datetime.now() - self.time_started
            days = time_diff.days
            hours, remainder = divmod(time_diff.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime = f"{days}d {hours}h {minutes}m {seconds}s"
            message = f"Uptime: {uptime}"

            runner_event = await context.db.find_one(
                RunnerEvent,
                (RunnerEvent.runner_type == RunnerType.RESOURCE_RUNNER)
                & (RunnerEvent.resource == self.resource)
                & (RunnerEvent.event == RunnerEventEnum.ALIVE)
                & (RunnerEvent.pid == self.pid),
            )
            if runner_event:
                runner_event.message = message
                runner_event.timestamp = datetime.now()
                runner_event.git_status = make_git_list()
                await context.db.save(runner_event)
                return  # updated existing event

        runner_event = RunnerEvent(
            runner_type=RunnerType.RESOURCE_RUNNER,
            pid=self.pid,
            hostname=self.hostname,
            user=self.username,
            timestamp=datetime.now(),
            git_status=make_git_list(),
            event=event,
            resource=self.resource,
            message=message,
        )
        await context.db.save(runner_event)

    async def run_node(self, registry_entry: NodeRegistry):
        """Run a single node by its ID from the database"""
        await self.write_node_event(RunnerEventEnum.NODE_STARTED, registry_entry.id)
        try:
            logger.info(f"Running node task_id: {registry_entry.id} on resource {context.config.resource}")
            if (
                hasattr(registry_entry.parameters, "queue")
                and registry_entry.parameters.queue == "slurm-queue"
            ):
                await submit_node(registry_entry)
            else:
                return await run_node_from_registry(registry_entry)

        except Exception as e:
            logger.exception(
                f"Error running node task_id: {registry_entry.id} on resource {context.config.resource} : {str(e)}"
            )
            if registry_entry:
                registry_entry.status = TaskStatus.FAILED
                await context.db.save(registry_entry)
            return False

    async def run_nodes_for_resource(self, resource_name, polling_interval=5, max_concurrent=10):
        """Orchestrates multiple independent services"""
        # Save PID on startup
        project_dir = Path(__file__).resolve().parents[3]
        (project_dir / "runner.pid").write_text(str(os.getpid()))
        
        await self.write_resource_event(RunnerEventEnum.RUNNER_STARTED)

        self.services = [
            JobPollingService(self, resource_name, polling_interval, max_concurrent),
            RunnerStatusService(self, interval=60),
            SlurmStatusService(self, resource_name, interval=30),
            GitUvUpdateService(self, interval=60), # Advanced check every hour
        ]

        # Start all and wait for them
        tasks = [service.start() for service in self.services]
        
        try:
            # We use gather to keep the manager alive while services run
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await self.stop_all_services()
            raise


async def async_main(args):
    """Async entry point"""
    context.initialize(resource=args.resource, db_name=args.db_name)
    if args.resource:
        context.config.resource = Resource(value=args.resource)
        logger.info(f"Setting resource for runner to {args.resource}")
        runner_manager = RunnerManager(context.config.resource)

        await runner_manager.run_nodes_for_resource(args.resource, args.polling_interval, None)

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
