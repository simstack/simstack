import argparse
import asyncio
import logging
import os

from simstack.core.definitions import DBType

from simstack.core.context import context
from simstack.models.resource_definition import ResourceDefinition
from simstack.tables.node_table import make_node_table
from simstack.tables.model_table import make_model_table
from simstack.core.services.runner_manager import RunnerManager

logger = logging.getLogger("NodeRunner")


async def initialize_default_resource() -> ResourceDefinition | None:
    """
    Checks if the current resource is the default one.
    If so, syncs the node and model tables from entry points and optional dirs.
    """
    resource_def = await context.db.find_one(
        ResourceDefinition,
        ResourceDefinition.resource_str == str(context.config.resource),
    )

    if resource_def is None:
        logger.warning(
            "No ResourceDefinition found for '%s'; skipping default-resource initialization.",
            str(context.config.resource),
        )
        return None

    if resource_def.is_default:
        logger.info("Default resource: initializing model and node tables")
        try:
            await make_model_table(context.db)
            await make_node_table(context.db)
        except Exception as e:
            logger.error(f"Failed to initialize default resource tables: {e}")

    return resource_def


async def async_main(args: argparse.Namespace) -> None:
    """Async entry point"""
    if args.connection_string == "none" or args.db_name == "none":
        await context.initialize(resource=args.resource, config_file=args.config)
    else:
        await context.initialize(
            resource=args.resource,
            db_name=args.db_name,
            connection_string=args.connection_string,
            db_type=DBType.MONGODB,
            config_file=args.config,
        )

    # Initialize tables if this is the default resource
    resource_def = await initialize_default_resource()
    is_default_resource = bool(resource_def and resource_def.is_default)

    if args.resource:
        logger.info(f"Setting resource for runner to {args.resource}")
        runner_manager = RunnerManager(
            context.config.resource,
            detach=args.detach,
            no_pull=args.no_pull,
            is_default=is_default_resource,
            with_file_transfer=args.with_file_transfer,
        )
        await runner_manager.run_nodes_for_resource(
            args.polling_interval, 10, timeout=args.timeout
        )


def runner_main() -> None:
    parser = argparse.ArgumentParser(description="Run nodes for a specific resource")
    parser.add_argument(
        "--config",
        type=str,
        # default="config.toml",
        help="Path to the configuration file",
    )

    parser.add_argument(
        "--resource",
        type=str,
        default="local",
        help="Resource name to process tasks for",
    )

    parser.add_argument(
        "--db-name",
        type=str,
        default="none",
        help="Specify a non-standard database",
    )

    parser.add_argument(
        "--connection-string",
        type=str,
        default="none",
        help="Specify a non-standard connection string",
    )

    parser.add_argument(
        "--polling-interval",
        type=int,
        default=20,
        help="Interval in seconds between polling for new tasks",
    )

    parser.add_argument(
        "--detach",
        type=lambda x: (str(x).lower() not in ["false", "0", "no"]),
        default=True,
        help="If true (default), run nodes in an external process. Set to 'false' to run inline.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Timeout in minutes after which the runner will terminate",
    )

    parser.add_argument(
        "--no-pull",
        action="store_true",
        default=False,
        help="If true, do not pull from git (GitUvUpdateService will not be started)",
    )

    parser.add_argument(
        "--with-file-transfer",
        type=lambda x: (str(x).lower() not in ["false", "0", "no"]),
        default=True,
        help="If true (default), start the FileTransferService.",
    )

    args = parser.parse_args()
    # Run the async main function
    asyncio.run(async_main(args))
    pid = os.getpid()
    logger.info(f"runner with pid {pid} shutting down normally")


if __name__ == "__main__":
    runner_main()
