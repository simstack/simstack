import argparse
import asyncio
import logging

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_database

logger = logging.getLogger("SlurmRunner")


async def run_node(node_id: str, **kwargs):
    """Run a single node by its ID from the database"""
    registry_entry = None
    try:
        registry_entry = await context.db.load_task_by_id(node_id)

        if not registry_entry:
            logger.error(f"Node with ID {node_id} not found in the database")
            return False
        # Create node from the registry entry
        node = await node_from_database(registry_entry)
        if not node:
            logger.error(
                f"Failed to create node from registry entry task_id: {registry_entry.id}"
            )
            registry_entry.status = TaskStatus.FAILED
            await context.db.save(registry_entry)
            return False
        registry_entry = node.registry_entry # it may have changed
        # if the node was recovered we do not have to run it again
        if node.status == TaskStatus.SUBMITTED or node.status == TaskStatus.SLURM_QUEUED or node.status == TaskStatus.SLURM_QUEUED:
            await node.execute_node_locally()
        else:
            logger.info(f"task_id: {registry_entry.id} skipping task: {registry_entry.name} with status {registry_entry.status}")
        return node.status == TaskStatus.COMPLETED
    except Exception as e:
        logger.exception(f"Error running node task_id: {node_id}: {str(e)}")
        if registry_entry:
            registry_entry.status = TaskStatus.FAILED
            await context.db.save(registry_entry)
        return False

def run_node_main():
    parser = argparse.ArgumentParser(description="Run nodes for a specific resource")
    parser.add_argument(
        "--node-id",
        type=str,
        help="Specific node ID to run (overrides resource-based polling)",
    )

    parser.add_argument(
        "--resource",
        default="local",
        nargs="?",
        type=str,
        help="resource to load",
    )

    args = parser.parse_args()
    context.initialize(resource=args.resource)

    if args.node_id:
        # Run a specific node once
        asyncio.run(run_node(args.node_id))


if __name__ == "__main__":
    run_node_main()
