import argparse
import asyncio
import logging
import os
import json
import sys
from pathlib import Path

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
    if args.connection_string not in (None, "none") or args.db_name not in (None, "none"):
        raise ValueError("Runner database credentials are no longer supported; use --server and --token")
    if getattr(args, "credentials", None):
        from simstack.runner_api.credentials import read_credentials
        source = Path(getattr(args, "credentials", None)).expanduser().resolve()
        credentials = read_credentials(source)
        os.environ["SIMSTACK_RUNNER_CREDENTIALS"] = str(source)
        os.environ["SIMSTACK_SERVER_URL"] = credentials["server_url"]
        os.environ["SIMSTACK_RUNNER_TOKEN"] = credentials["token"]
    if (getattr(args, "server", None) or getattr(args, "token", None)) and not getattr(args, "credentials", None):
        os.environ.pop("SIMSTACK_RUNNER_CREDENTIALS", None)
    if getattr(args, "server", None):
        os.environ["SIMSTACK_SERVER_URL"] = getattr(args, "server", None)
    if getattr(args, "token", None):
        os.environ["SIMSTACK_RUNNER_TOKEN"] = getattr(args, "token", None)
    project_root = Path(getattr(args, "project_root", None) or Path.home() / ".local/share/simstack/runner/runtime").expanduser().resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    await context.initialize(resource=args.resource, config_file=args.config, project_root=project_root, api_only=True)
    resource = context.config.resource
    runner_manager = RunnerManager(
        resource, detach=args.detach, no_pull=True, is_default=False,
        with_file_transfer=args.file_transfer, with_jobs=args.jobs,
    )
    await runner_manager.run_nodes_for_resource(args.polling_interval, 10, timeout=args.timeout)
    return bool(getattr(runner_manager, "_restart_requested", False))


def runner_main() -> None:
    parser = argparse.ArgumentParser(description="Run nodes through the SimStack server API")
    parser.add_argument("--server", help="Server API URL, for example https://simstack.site/api")
    parser.add_argument("--token", help="Resource runner API token")
    parser.add_argument("--credentials", help="Path to a private JSON credentials file")
    parser.add_argument("--project-root", help="Local runner runtime directory")
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
        "--pull",
        type=lambda x: (str(x).lower() not in ["false", "0", "no"]),
        default=True,
        help="If true (default), pull from git (GitUvUpdateService will be started). Set to 'false' to skip.",
    )

    parser.add_argument(
        "--file-transfer",
        type=lambda x: (str(x).lower() not in ["false", "0", "no"]),
        default=True,
        help="If true (default), start the FileTransferService. Set to 'false' to skip.",
    )

    parser.add_argument(
        "--jobs",
        type=lambda x: (str(x).lower() not in ["false", "0", "no"]),
        default=True,
        help=(
            "If true (default), start NodeExecutionService and SlurmStatusService. "
            "Set to 'false' to skip job claiming (file-transfer sidecar)."
        ),
    )

    args = parser.parse_args()
    # Run the async main function
    restart_requested = asyncio.run(async_main(args))
    if restart_requested:
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, "-m", "simstack.core.runner", *sys.argv[1:]])
    pid = os.getpid()
    logger.info(f"runner with pid {pid} shutting down normally")


if __name__ == "__main__":
    runner_main()
