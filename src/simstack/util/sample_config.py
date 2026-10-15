"""Write an API-only runner connection file without secrets or host assumptions."""

import argparse
import json
from pathlib import Path

from simstack.runner_api.config import RunnerConnectionSettings
from simstack.runner_api.errors import RunnerApiConfigurationError


def sample_config_writer_entrypoint() -> None:
    """Generate runner.toml; execution policy remains an operator-owned file."""
    parser = argparse.ArgumentParser(
        description="Write a secret-free API-only runner connection configuration"
    )
    parser.add_argument("--server-url", required=True, help="Absolute server API URL")
    parser.add_argument("--resource", required=True, help="Saved resource name")
    parser.add_argument("--output", type=Path, default=Path("runner.toml"))
    args = parser.parse_args()
    try:
        connection = RunnerConnectionSettings.from_mapping(
            {"runner": {"server_url": args.server_url, "resource": args.resource}}
        )
    except RunnerApiConfigurationError as exc:
        parser.error(str(exc))
    configuration = "\n".join(
        (
            "# API-only runner connection. Keep SIMSTACK_RUNNER_TOKEN in the environment.",
            "# Worker launch policy belongs in a separate operator-owned config.toml.",
            "[runner]",
            f"server_url = {json.dumps(connection.server_url, ensure_ascii=False)}",
            f"resource = {json.dumps(connection.resource, ensure_ascii=False)}",
            "",
        )
    )
    try:
        with args.output.open("x", encoding="utf-8") as configuration_file:
            configuration_file.write(configuration)
    except FileExistsError:
        parser.error("Output already exists; refusing to overwrite it")
    except OSError:
        parser.error("Runner connection configuration cannot be written")
    print(f"Runner connection configuration written to {args.output}")


if __name__ == "__main__":
    sample_config_writer_entrypoint()
