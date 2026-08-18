import argparse
import asyncio
import logging
import sys

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.repository_task_runtime import repository_task_initialization
from simstack.core.services.node_execution_service import run_node_from_registry

logger = logging.getLogger("Run Node")

async def run_node_from_id(node_id: str, resource_str: str, project_root: str = None):
    """Run a single node by its ID from the database"""
    logger.info(f"Initializing node run: node_id={node_id}, resource={resource_str}, project_root={project_root}")
    try:
        initialization = repository_task_initialization(
            resource=resource_str,
            project_root=project_root,
        )
        await context.initialize(**initialization)
    except Exception as e:
        logger.error(f"Failed to initialize context: {str(e)}", exc_info=True)
        return False
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
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', stream=sys.stdout)
    logger.info("Starting run_node_main")
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

    parser.add_argument(
        "--project-root",
        default=None,
        nargs="?",
        type=str,
        help="project root directory",
    )

    args = parser.parse_args()


    if args.node_id:
        # Run a specific node once
        asyncio.run(run_node_from_id(args.node_id, args.resource, args.project_root))


if __name__ == "__main__":
    run_node_main()
