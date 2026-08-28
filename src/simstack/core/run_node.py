import argparse
import asyncio
import logging
import os
import sys

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.run_docker import CONTAINER_WORKDIR, run_docker_with_outcome
from simstack.core.run_node_protocol import RunNodeResult, encode_run_node_result
from simstack.core.services.node_execution_service import (
    run_node_from_registry_with_outcome,
)
from simstack.util.sanitized_output import sanitized_tail

logger = logging.getLogger("Run Node")


def _configured_connection_string() -> str | None:
    try:
        configured = context.config.connection_string
    except (AttributeError, RuntimeError):
        configured = None
    return configured or os.environ.get("SIMSTACK_DB_CONNECTION_STRING")


def _clean_error(error: object) -> str:
    return sanitized_tail(str(error), _configured_connection_string()) or type(
        error
    ).__name__


async def run_node_from_id(
    node_id: str,
    resource_str: str,
    project_root: str | None = None,
    in_docker: bool = False,
) -> RunNodeResult:
    """Run a single node by its ID from the database"""
    logger.info(f"Initializing node run: node_id={node_id}, resource={resource_str}, project_root={project_root}, in_docker={in_docker}")
    try:
        init_kwargs = {
            "resource": resource_str,
            "project_root": project_root,
            "in_docker": bool(in_docker),
        }
        # Host workdirs (e.g. C:/Users/...) are bind-mounted at a neutral path.
        if in_docker:
            init_kwargs["workdir"] = CONTAINER_WORKDIR
        await context.initialize(**init_kwargs)
    except (Exception, SystemExit) as e:
        error = _clean_error(e)
        logger.error("Failed to initialize context: %s", error)
        return RunNodeResult(False, "none", error)
    registry_entry = None
    try:
        registry_entry = await context.db.load_task_by_id(node_id)
        if not registry_entry:
            logger.error(f"Node with ID {node_id} not found in the database")
            return RunNodeResult(False, "none", f"Node {node_id} not found")
        parameters = registry_entry.parameters
        if parameters.in_docker and not in_docker:
            docker_result = await run_docker_with_outcome(registry_entry)
            if not docker_result.success:
                return RunNodeResult(
                    False, docker_result.return_kind, docker_result.error
                )
            updated_entry = await context.db.load_task_by_id(node_id)
            if updated_entry is None or updated_entry.status != TaskStatus.COMPLETED:
                error = "Docker child exited successfully without completing the task"
                return RunNodeResult(False, "none", error)
            return RunNodeResult(True, docker_result.return_kind)
        outcome = await run_node_from_registry_with_outcome(registry_entry)
        return RunNodeResult(outcome.success, outcome.return_kind)
    except (Exception, SystemExit) as e:
        error = _clean_error(e)
        logger.error("Error running node task_id: %s: %s", node_id, error)
        if registry_entry:
            registry_entry.status = TaskStatus.FAILED
            registry_entry.error = error
            registry_entry.return_kind = "exception"
            await context.db.save(registry_entry)
        return RunNodeResult(False, "exception", error)


def run_node_main() -> None:
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
        result = asyncio.run(
            run_node_from_id(
                args.node_id,
                args.resource,
                args.project_root,
                args.in_docker,
            )
        )
        print(encode_run_node_result(result))
        if not result.success:
            raise SystemExit(1)


if __name__ == "__main__":
    run_node_main()
