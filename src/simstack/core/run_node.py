import argparse
import asyncio
import logging
import sys

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.run_docker import run_docker
from simstack.core.services.node_execution_service import run_node_from_registry

logger = logging.getLogger("Run Node")

async def run_node_from_id(node_id: str, resource_str: str, project_root: str = None, in_docker: bool = False):
    """Run a single node by its ID from the database"""
    logger.info(f"Initializing node run: node_id={node_id}, resource={resource_str}, project_root={project_root}, in_docker={in_docker}")
    try:
        init_kwargs = {
            "resource": resource_str,
            "project_root": project_root,
            "in_docker": in_docker,
        }
        # Host workdirs (e.g. C:/Users/...) are bind-mounted at /root/simstack.
        if in_docker:
            init_kwargs["workdir"] = "/root/simstack"
        await context.initialize(**init_kwargs)
    except Exception as e:
        logger.error(f"Failed to initialize context: {str(e)}", exc_info=True)
        return False
    registry_entry = None
    try:
        registry_entry = await context.db.load_task_by_id(node_id)
        if not registry_entry:
            logger.error(f"Node with ID {node_id} not found in the database")
            return False
        queue = registry_entry.parameters.queue
        if (queue == "docker" or queue == "slurm-docker") and not in_docker:
            return await run_docker(registry_entry)
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

    parser.add_argument(
        "--in-docker",
        action="store_true",
        help="run inside a docker container",
    )

    args = parser.parse_args()

    if args.node_id:
        # Run a specific node once
        asyncio.run(run_node_from_id(args.node_id, args.resource, args.project_root, args.in_docker))


if __name__ == "__main__":
    run_node_main()
