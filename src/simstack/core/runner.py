import argparse
import asyncio
import logging
import os

from simstack.core.context import context
from simstack.core.services.runner_manager import RunnerManager
from simstack.runner_api.config import RunnerConnectionSettings
from simstack.runner_api.errors import RunnerApiConfigurationError
from simstack.runner_runtime.environment import (
    discard_runner_token_from_environment,
    sanitize_runner_process_environment,
)

logger = logging.getLogger("NodeRunner")


async def async_main(args: argparse.Namespace) -> None:
    """Start an API-only runner. Missing API credentials abort startup."""
    connection_kwargs = {}
    resource = args.resource
    if connection_path := getattr(args, "connection_config", None):
        connection = RunnerConnectionSettings.from_file(connection_path)
        if resource is not None and resource != connection.resource:
            raise RunnerApiConfigurationError(
                "--resource does not match the runner connection configuration"
            )
        resource = connection.resource
        # An explicitly selected file is authoritative; no fallback to another
        # server or legacy configuration if its bootstrap fails.
        connection_kwargs["server_url"] = connection.server_url
    sanitize_runner_process_environment(
        allowed_environment_names=tuple(args.allow_environment)
    )
    await context.initialize_runner(
        resource=resource,
        config_file=args.config,
        project_root=args.project_root,
        workdir_override=args.workdir,
        with_file_transfer=args.with_file_transfer,
        **connection_kwargs,
    )
    discard_runner_token_from_environment()

    resource = context.config.resource
    logger.info("Setting resource for runner to %s", resource)
    runner_manager = RunnerManager(
        resource,
        is_default=context.config.is_default,
        with_file_transfer=args.with_file_transfer,
    )
    await runner_manager.run_nodes_for_resource(
        args.polling_interval, 10, timeout=args.timeout
    )


def runner_main() -> None:
    parser = argparse.ArgumentParser(description="Run nodes for a specific resource")
    parser.add_argument(
        "--connection-config",
        type=str,
        help=(
            "Path to secret-free runner.toml downloaded from the UI; "
            "the resource token still comes from SIMSTACK_RUNNER_TOKEN"
        ),
    )
    parser.add_argument(
        "--config",
        type=str,
        help=(
            "Path to operator-owned execution-adapter config.toml "
            "(defaults to <project-root>/config.toml); not the connection file"
        ),
    )

    parser.add_argument(
        "--resource",
        type=str,
        default=None,
        help="Expected token resource (defaults to the resource in the runner token)",
    )

    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Local checkout root (auto-detected by default)",
    )

    parser.add_argument(
        "--workdir",
        type=str,
        default=None,
        help=(
            "Explicit runner work directory override; useful when the "
            "server-owned path is mounted elsewhere in an execution container"
        ),
    )

    parser.add_argument(
        "--polling-interval",
        type=int,
        default=20,
        help="Interval in seconds between polling for new tasks",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Timeout in minutes after which the runner will terminate",
    )

    parser.add_argument(
        "--with-file-transfer",
        type=lambda x: (str(x).lower() not in ["false", "0", "no"]),
        default=True,
        help="If true (default), start the FileTransferService. Set to 'false' to skip.",
    )

    parser.add_argument(
        "--allow-environment",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Explicitly preserve one non-SimStack environment variable for "
            "configured workers; may be repeated"
        ),
    )

    args = parser.parse_args()
    # Run the async main function
    asyncio.run(async_main(args))
    logging.shutdown()
    pid = os.getpid()
    logger.info(f"runner with pid {pid} shutting down normally")


if __name__ == "__main__":
    runner_main()
