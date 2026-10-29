"""Write a complete API-only Runner config with portable local execution defaults."""

import argparse
from pathlib import Path

from simstack.runner_api.config import render_runner_config
from simstack.runner_api.errors import RunnerApiConfigurationError


def sample_config_writer_entrypoint() -> None:
    """Generate one config.toml containing connection and basic execution settings."""
    parser = argparse.ArgumentParser(
        description="Write a complete secret-free API Runner configuration"
    )
    parser.add_argument("--server-url", required=True, help="Absolute server API URL")
    parser.add_argument("--resource", required=True, help="Saved resource name")
    parser.add_argument("--output", type=Path, default=Path("config.toml"))
    args = parser.parse_args()
    try:
        configuration = render_runner_config(args.server_url, args.resource)
    except RunnerApiConfigurationError as exc:
        parser.error(str(exc))
    try:
        with args.output.open("x", encoding="utf-8") as configuration_file:
            configuration_file.write(configuration)
    except FileExistsError:
        parser.error("Output already exists; refusing to overwrite it")
    except OSError:
        parser.error("Runner configuration cannot be written")
    print(f"Runner configuration written to {args.output}")


if __name__ == "__main__":
    sample_config_writer_entrypoint()
