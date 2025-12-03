import argparse
import asyncio
import logging

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node_from_database
from simstack.core.runner import run_node_from_registry

logger = logging.getLogger("Run Node")

async def run_node_from_id(node_id: str):
    """Run a single node by its ID from the database"""
    registry_entry = None
    try:
        registry_entry = await context.db.load_task_by_id(node_id)
        if not registry_entry:
            logger.error(f"Node with ID {node_id} not found in the database")
            return False
        return await run_node_from_registry(registry_entry)
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
        asyncio.run(run_node_from_id(args.node_id))


if __name__ == "__main__":
    run_node_main()
